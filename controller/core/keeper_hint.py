"""The Browser API's side of per-session proxy rotation, and pause awareness.

A local browser (one of this workspace's own, behind its Service) may run the
control sidecar on port 9300. After a session starts on it, the Browser API
tells the sidecar how many OTHER sessions there were recently used; with the
person's "rotate per session" setting the sidecar then moves to the next
proxy. A browser without the sidecar (refused, 404) is skipped silently, and
remote browsers are never called.

A browser without the sidecar has no port 9300 on its Service, and on this
network such a connection is dropped, not refused: without a short connect
bound every session start on it would wait the whole rotation timeout. So the
connect is bounded on its own, and a browser that didn't answer is not asked
again until its connection changes (the pod restarted, perhaps with the
sidecar) or NO_SIDECAR_SECONDS pass.

A browser whose profile is being copied closes Chrome for a few seconds; its
launcher answers /health {"status":"paused"}, and the picker treats it as away
instead of preferring it for having no open tabs.
"""
import asyncio
import logging
import time
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

KEEPER_PORT = 9300
LAUNCHER_PORT = 9000
SESSION_START_TIMEOUT = 120.0
# Reaching the sidecar, apart from the (long) wait for a rotation.
CONNECT_TIMEOUT = 2.0
NO_SIDECAR_SECONDS = 600.0
HEALTH_TIMEOUT = 2.0
HEALTH_CACHE_SECONDS = 2.0
# A session used within this window blocks a per-session rotation.
RECENT_SESSION_SECONDS = 600.0


def local_host(ws_url: Optional[str]) -> Optional[str]:
    """The Service host of a workspace browser, or None for a remote one."""
    if not ws_url:
        return None
    try:
        u = urlsplit(ws_url)
    except ValueError:
        return None
    host = u.hostname or ""
    if u.scheme != "ws" or not host.endswith(".svc.cluster.local"):
        return None
    return host


# host -> (monotonic time, connection token) of a sidecar that didn't answer.
_no_sidecar: Dict[str, Tuple[float, Optional[int]]] = {}


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(SESSION_START_TIMEOUT, connect=CONNECT_TIMEOUT)


async def session_start(
    host: str,
    open_sessions: int,
    client: Optional[httpx.AsyncClient] = None,
    conn: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Tell the browser's control sidecar a session started. Returns
    (rotated, reason); (False, None) when the sidecar isn't there.

    ``conn`` identifies the controller's current connection to that browser;
    a sidecar remembered as missing is tried again once it changes.
    """
    seen = _no_sidecar.get(host)
    if seen is not None and seen[1] == conn and time.monotonic() - seen[0] < NO_SIDECAR_SECONDS:
        return False, None
    url = f"http://{host}:{KEEPER_PORT}/v1/session-start"
    own = client is None
    client = client or httpx.AsyncClient(timeout=_timeout())
    try:
        resp = await client.post(url, json={"openSessions": open_sessions}, timeout=_timeout())
        _no_sidecar.pop(host, None)
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        return bool(data.get("rotated")), data.get("reason")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _no_sidecar[host] = (time.monotonic(), conn)
        return False, None
    except (httpx.HTTPError, ValueError, OSError):
        return False, None
    finally:
        if own:
            await client.aclose()


class PauseWatch:
    """Cached launcher /health reads for local browsers that are disconnected."""

    def __init__(self):
        self._seen: Dict[str, Tuple[float, bool]] = {}
        self._client: Optional[httpx.AsyncClient] = None

    def paused(self, browser_id: str) -> bool:
        seen = self._seen.get(browser_id)
        return bool(seen and seen[1] and time.monotonic() - seen[0] < HEALTH_CACHE_SECONDS * 5)

    def forget(self, browser_id: str) -> None:
        self._seen.pop(browser_id, None)

    async def _read(self, browser_id: str, host: str) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=HEALTH_TIMEOUT)
        paused = False
        try:
            resp = await self._client.get(f"http://{host}:{LAUNCHER_PORT}/health", timeout=HEALTH_TIMEOUT)
            if resp.status_code == 200:
                paused = (resp.json() or {}).get("status") == "paused"
        except (httpx.HTTPError, ValueError, OSError):
            paused = False
        self._seen[browser_id] = (time.monotonic(), paused)

    async def refresh(self, browsers: Iterable[Tuple[str, Optional[str]]]) -> None:
        """Re-read /health for (browser_id, host) pairs whose cache is stale."""
        now = time.monotonic()
        todo = []
        for bid, host in browsers:
            if not host:
                continue
            seen = self._seen.get(bid)
            if seen is None or now - seen[0] >= HEALTH_CACHE_SECONDS:
                todo.append(self._read(bid, host))
        if todo:
            await asyncio.gather(*todo, return_exceptions=True)


pause_watch = PauseWatch()
