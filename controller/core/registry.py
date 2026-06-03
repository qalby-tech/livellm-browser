import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Path to the operator-maintained browser registry (a mounted ConfigMap).
# JSON shape: {"browsers": {"<browser_id>": "ws://<svc>:<port>/devtools/browser/<id>", ...}}
# A bare {"<id>": "ws://..."} object is also accepted.
BROWSERS_CONFIG_PATH = os.environ.get("BROWSERS_CONFIG", "/etc/livellm/browsers.json")


class BrowserRegistry:
    """
    Static, file-backed browser registry — replaces the old Redis discovery.

    In the managed platform each browser is its own pod fronted by a stable
    Service, so a browser's CDP ws_url is a deterministic value that never
    drifts (the in-pod CDP proxy keeps a fixed port and rewrites the ws path
    across Chrome restarts, and the Service keeps a stable DNS name across pod
    restarts). The operator lists the namespace's Browser CRs and writes them
    into a ConfigMap mounted at ``BROWSERS_CONFIG_PATH``; we read it on demand
    (cached by mtime) so operator updates propagate without a restart.

    When the file is absent (standalone / tests) the registry is empty and
    browsers can still be registered ad-hoc via ``POST /parser/browsers``.
    """

    def __init__(self, path: str = BROWSERS_CONFIG_PATH):
        self.path = path
        # {browser_id: {"wsUrl": str, "headers": {name: value}}}
        self._cache: dict[str, dict] = {}
        self._mtime: float = -1.0

    @staticmethod
    def _normalize(value) -> Optional[dict]:
        """Accept a plain ws_url string OR an object {wsUrl, headers}.

        Returns a normalized {"wsUrl": str, "headers": dict} or None if invalid.
        The object form carries optional auth headers for BYO/remote browsers
        (e.g. {"wsUrl": "wss://…", "headers": {"Authorization": "Bearer …"}}).
        """
        if isinstance(value, str):
            return {"wsUrl": value, "headers": {}} if value else None
        if isinstance(value, dict):
            ws = value.get("wsUrl") or value.get("ws_url") or ""
            raw_headers = value.get("headers") or {}
            headers = {}
            if isinstance(raw_headers, dict):
                headers = {str(k): str(v) for k, v in raw_headers.items() if v}
            return {"wsUrl": str(ws), "headers": headers} if ws else None
        return None

    def _load(self) -> dict[str, dict]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self._cache:
                logger.info(f"registry: {self.path} disappeared, registry now empty")
            self._cache = {}
            self._mtime = -1.0
            return self._cache
        except OSError as e:
            logger.warning(f"registry: stat {self.path} failed: {e}")
            return self._cache

        if st.st_mtime == self._mtime:
            return self._cache

        try:
            with open(self.path) as f:
                data = json.load(f)
            browsers = data.get("browsers", data) if isinstance(data, dict) else {}
            cache = {}
            for k, v in browsers.items():
                entry = self._normalize(v)
                if entry:
                    cache[str(k)] = entry
            self._cache = cache
            self._mtime = st.st_mtime
            logger.info(
                f"registry: loaded {len(self._cache)} browser(s) from {self.path}"
            )
        except (json.JSONDecodeError, OSError, ValueError, AttributeError) as e:
            logger.warning(f"registry: failed to parse {self.path}: {e}")
        return self._cache

    def get_all_browsers(self) -> dict[str, str]:
        """Return {browser_id: ws_url} from the registry file."""
        return {bid: e["wsUrl"] for bid, e in self._load().items()}

    def get_browser_ws_url(self, browser_id: str) -> Optional[str]:
        """Resolve a browser's CDP ws_url, or None if it isn't in the registry."""
        entry = self._load().get(browser_id)
        return entry["wsUrl"] if entry else None

    def get_browser_headers(self, browser_id: str) -> dict:
        """Auth headers to send on CDP connect for this browser (may be empty)."""
        entry = self._load().get(browser_id)
        return dict(entry["headers"]) if entry else {}


browser_registry = BrowserRegistry()
