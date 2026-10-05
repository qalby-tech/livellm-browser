import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Path to the operator-maintained browser registry (a mounted ConfigMap).
# JSON shape: {"browsers": {"<browser_id>": "ws://<svc>:<port>/devtools/browser/<id>", ...}}
# An entry may also be an object {"wsUrl", "headers"?, "engine"?}: a Camoufox
# browser's is {"wsUrl": "ws://<svc>:9222/playwright/default", "engine": "camoufox"}
# (no engine = a Chrome browser's CDP address).
BROWSERS_CONFIG_PATH = os.environ.get("BROWSERS_CONFIG", "/etc/livellm/browsers.json")


def managed() -> bool:
    """Whether the registry decides which browsers this controller drives.

    Keyed on the BROWSERS_CONFIG env var (the operator and compose always set
    it), not on the file: the mount can be briefly absent at startup, and the
    answer must not flap. Managed, a browser that leaves the registry leaves
    the controller, and POST/DELETE /browsers are refused.
    """
    return "BROWSERS_CONFIG" in os.environ


class BrowserRegistry:
    """
    Static, file-backed browser registry.

    In the managed platform each browser is its own pod fronted by a stable
    Service, so a browser's CDP ws_url is a deterministic value that never
    drifts (the in-pod CDP proxy keeps a fixed port and rewrites the ws path
    across Chrome restarts, and the Service keeps a stable DNS name across pod
    restarts). The operator lists the namespace's Browser CRs and writes them
    into a ConfigMap mounted at ``BROWSERS_CONFIG_PATH``; we read it on demand
    (cached by mtime) so operator updates propagate without a restart.

    When BROWSERS_CONFIG is unset (standalone / tests) the registry is empty and
    browsers can be registered ad-hoc via ``POST /parser/browsers``.
    """

    def __init__(self, path: str = BROWSERS_CONFIG_PATH):
        self.path = path
        # {browser_id: {"wsUrl": str, "headers": {name: value}, "engine": str}}
        self._cache: dict[str, dict] = {}
        self._mtime: float = -1.0

    @staticmethod
    def _normalize(value) -> Optional[dict]:
        """Accept a plain ws_url string OR an object {wsUrl, headers, engine}.

        Returns a normalized {"wsUrl": str, "headers": dict, "engine": str} or
        None if invalid. The object form carries optional auth headers for
        BYO/remote browsers (e.g. {"wsUrl": "wss://…", "headers":
        {"Authorization": "Bearer …"}}) and a Camoufox browser's engine
        ({"wsUrl": "ws://…/playwright/default", "engine": "camoufox"}); any
        other engine, or none, is Chrome.
        """
        if isinstance(value, str):
            return {"wsUrl": value, "headers": {}, "engine": "chrome"} if value else None
        if isinstance(value, dict):
            ws = value.get("wsUrl") or ""
            raw_headers = value.get("headers") or {}
            headers = {}
            if isinstance(raw_headers, dict):
                headers = {str(k): str(v) for k, v in raw_headers.items() if v}
            engine = "camoufox" if value.get("engine") == "camoufox" else "chrome"
            return {"wsUrl": str(ws), "headers": headers, "engine": engine} if ws else None
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
            browsers = data.get("browsers", {}) if isinstance(data, dict) else {}
            cache = {}
            for k, v in browsers.items():
                entry = self._normalize(v)
                if entry:
                    cache[str(k)] = entry
                    raw = v.get("engine") if isinstance(v, dict) else None
                    if raw and raw not in ("chrome", "camoufox"):
                        # Still Chrome, as an entry without one: said once per
                        # load, or a mistyped engine reads only as "not
                        # reachable" later.
                        logger.warning(
                            f"registry: browser {k!r} names an unknown engine {raw!r}; "
                            "it is driven as a Chrome browser"
                        )
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

    def get_browser_engine(self, browser_id: str) -> Optional[str]:
        """"chrome" or "camoufox" for a browser in the registry, else None."""
        entry = self._load().get(browser_id)
        return entry["engine"] if entry else None


browser_registry = BrowserRegistry()
