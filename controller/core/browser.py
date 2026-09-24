import asyncio
import logging
import os
import signal
import time
from typing import Iterable, Optional

from patchright.async_api import Playwright, Browser, BrowserContext, Page

from core.config import settings

logger = logging.getLogger(__name__)

# Soft cap: a browser with this many open tabs is picked only when every
# other one is as busy. Never a refusal.
MAX_PAGES_PER_BROWSER = settings.max_pages_per_browser

# How long a browser that could not be reached stays out of the pick.
UNHEALTHY_SECONDS = 30.0

# Closing tabs over a connection whose browser is gone can stall.
DISCONNECT_TIMEOUT = 10.0


class BrowserInfo:
    """Container for a connected browser, its default context, and active pages."""

    def __init__(self, browser: Browser, context: BrowserContext, ws_url: str = "", browser_id: str = "", headers: Optional[dict] = None):
        self.browser = browser
        self.context = context
        self.ws_url = ws_url
        self.browser_id = browser_id
        # Optional auth headers sent on CDP connect (BYO/remote browsers).
        self.headers = headers or {}
        # Tabs of the sessions that live on this browser, by session id.
        # Emptied when the connection is rebuilt; the session itself stays
        # on this browser (BrowserManager.sessions) and gets a new tab.
        self.pages: dict[str, Page] = {}


class BrowserManager:
    """
    Agnostic browser manager — connects to browsers purely via CDP WebSocket URLs.

    The manager does NOT know about launchers, profiles, or orchestration.
    External systems (operator, API calls) register browsers by providing a
    ``browser_id`` and a ``ws_url``.
    """

    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browsers: dict[str, BrowserInfo] = {}
        # session id -> browser id. Kept apart from BrowserInfo.pages so a
        # session outlives a reconnect of its browser: it stays there and
        # gets a new tab. Dropped on end_session and when the browser leaves.
        self.sessions: dict[str, str] = {}
        # Calls running on each browser, counted from the moment the browser
        # is chosen until the response is sent (see pick_browser/end_call).
        self._in_flight: dict[str, int] = {}
        # browser id -> monotonic deadline; a browser that failed to connect
        # is skipped by pick_browser until then.
        self._unhealthy_until: dict[str, float] = {}
        self._rr = 0
        self._reconnect_lock = asyncio.Lock()
        self._playwright_pid: Optional[int] = None

    async def start(self, playwright: Playwright):
        """Initialise with a Playwright instance. No auto-connections."""
        self.playwright = playwright
        self._track_playwright_pid()
        logger.info("Browser manager started (agnostic mode — waiting for registrations)")

    def _driver_proc(self):
        """The Node driver subprocess behind the pipe transport, or None.

        Reaches through private attributes (impl connection -> pipe transport),
        so every step is guarded — a layout change just disables the feature.
        """
        try:
            return self.playwright._impl_obj._connection._transport._proc
        except AttributeError:
            return None

    def _track_playwright_pid(self):
        proc = self._driver_proc()
        if proc is not None:
            self._playwright_pid = proc.pid
            logger.info(f"Tracking Playwright driver PID: {self._playwright_pid}")

    def driver_alive(self) -> bool:
        """Whether the Playwright Node driver process is still running.

        Returns True when the process can't be introspected (private layout
        changed) so a false negative can never crash-loop the pod.
        """
        if not self.playwright:
            return False
        proc = self._driver_proc()
        if proc is None:
            return True
        # returncode is None while running, an int once exited. Only a
        # definite int counts as dead (doubles/mocks stay "alive").
        return not isinstance(proc.returncode, int)

    def _kill_playwright_process(self):
        if self._playwright_pid:
            try:
                os.kill(self._playwright_pid, signal.SIGKILL)
                logger.warning(f"Force-killed old Playwright driver PID {self._playwright_pid}")
            except (ProcessLookupError, PermissionError):
                pass
            self._playwright_pid = None

    # ── connect / disconnect ─────────────────────────────────

    async def connect_browser(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Connect to a remote browser over CDP.

        If ``browser_id`` is already connected **with the same URL** and the
        connection is still alive, the existing connection is returned.
        If the connection is dead (e.g. browser restarted), it auto-reconnects.
        If the URL differs, the old connection is dropped and a new one opened.

        ``headers`` are optional HTTP headers sent on the CDP connect, used for
        BYO/remote browsers that require auth (e.g. an Authorization bearer).
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if browser_id in self.browsers:
            existing = self.browsers[browser_id]
            if existing.ws_url == ws_url and existing.browser.is_connected():
                logger.info(f"Browser '{browser_id}' already connected (idempotent)")
                return existing
            if existing.ws_url != ws_url:
                logger.info(
                    f"Browser '{browser_id}' ws_url changed "
                    f"({existing.ws_url} -> {ws_url}), reconnecting"
                )
            else:
                logger.info(f"Browser '{browser_id}' connection is dead, reconnecting...")
            await self.disconnect_browser(browser_id)

        logger.info(f"Connecting to browser '{browser_id}' via {ws_url}")
        browser = await self.playwright.chromium.connect_over_cdp(ws_url, headers=headers or None)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {})
        self.browsers[browser_id] = info
        self.mark_healthy(browser_id)
        logger.info(f"Connected to browser '{browser_id}'")
        return info

    async def disconnect_browser(self, browser_id: str) -> bool:
        """Disconnect a browser and close all its session pages."""
        if browser_id not in self.browsers:
            return False

        info = self.browsers.pop(browser_id)

        for page in list(info.pages.values()):
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page in '{browser_id}': {e}")

        try:
            await info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser '{browser_id}': {e}")

        logger.info(f"Disconnected browser '{browser_id}'")
        return True

    async def remove_browser(self, browser_id: str) -> bool:
        """Disconnect a browser for good: its sessions end with it.

        ``disconnect_browser`` alone keeps the sessions (a reconnect gives them
        new tabs on the same browser); this is for a browser that left.
        """
        for sid in [s for s, b in self.sessions.items() if b == browser_id]:
            del self.sessions[sid]
        self._unhealthy_until.pop(browser_id, None)
        present = browser_id in self.browsers
        try:
            await asyncio.wait_for(self.disconnect_browser(browser_id), timeout=DISCONNECT_TIMEOUT)
        except Exception as e:
            logger.warning(f"Error disconnecting '{browser_id}': {e}")
            self.browsers.pop(browser_id, None)
        return present

    # ── lookup helpers ───────────────────────────────────────

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a connected browser by ID. Raises ``KeyError`` if not found."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser '{browser_id}' not connected")
        return self.browsers[browser_id]

    # ── sessions ─────────────────────────────────────────────

    def session_browser(self, session_id: str) -> Optional[str]:
        """The browser a session lives on, or None for an unknown session."""
        return self.sessions.get(session_id)

    def add_session(self, browser_id: str, session_id: str, page: Page) -> None:
        self.sessions[session_id] = browser_id
        self.browsers[browser_id].pages[session_id] = page

    def end_session(self, session_id: str) -> Optional[Page]:
        """Forget a session; returns its tab (if it has one) for the caller to close."""
        browser_id = self.sessions.pop(session_id, None)
        info = self.browsers.get(browser_id) if browser_id else None
        return info.pages.pop(session_id, None) if info else None

    def session_count(self, browser_id: str) -> int:
        return sum(1 for b in self.sessions.values() if b == browser_id)

    # ── load and health ──────────────────────────────────────

    def is_connected(self, browser_id: str) -> bool:
        info = self.browsers.get(browser_id)
        if info is None:
            return False
        try:
            return bool(info.browser.is_connected())
        except Exception:
            return False

    def open_tabs(self, browser_id: str) -> int:
        """Every tab open in the browser: sessions, one-off calls and tabs a
        person opened. 0 for a browser that is not connected."""
        if not self.is_connected(browser_id):
            return 0
        info = self.browsers[browser_id]
        try:
            return sum(len(ctx.pages) for ctx in info.browser.contexts)
        except Exception:
            return len(info.pages)

    def load(self, browser_id: str) -> int:
        return self.open_tabs(browser_id) + self._in_flight.get(browser_id, 0)

    def begin_call(self, browser_id: str) -> None:
        self._in_flight[browser_id] = self._in_flight.get(browser_id, 0) + 1

    def end_call(self, browser_id: str) -> None:
        left = self._in_flight.get(browser_id, 0) - 1
        if left > 0:
            self._in_flight[browser_id] = left
        else:
            self._in_flight.pop(browser_id, None)

    def is_healthy(self, browser_id: str) -> bool:
        until = self._unhealthy_until.get(browser_id)
        return until is None or time.monotonic() >= until

    def mark_unhealthy(self, browser_id: str) -> None:
        self._unhealthy_until[browser_id] = time.monotonic() + UNHEALTHY_SECONDS

    def mark_healthy(self, browser_id: str) -> None:
        self._unhealthy_until.pop(browser_id, None)

    def pick_browser(self, pool: Iterable[str], exclude: Iterable[str] = ()) -> Optional[str]:
        """Choose a browser for a call that names none, and count the call on it.

        Fewest open tabs plus calls in flight wins. Browsers that recently
        failed to connect come last, then those at the soft tab cap; ties
        rotate. Picking and counting happen with no await in between, so
        calls that arrive together spread out. The caller must end_call().
        """
        skip = set(exclude)
        options = [b for b in pool if b not in skip]
        if not options:
            return None
        rank = {}
        for b in options:
            load = self.load(b)
            rank[b] = (not self.is_healthy(b), load >= MAX_PAGES_PER_BROWSER, load)
        best = min(rank.values())
        ties = [b for b in options if rank[b] == best]
        browser_id = ties[self._rr % len(ties)]
        self._rr += 1
        self.begin_call(browser_id)
        return browser_id

    async def ensure_connected(
        self, browser_id: str, ws_url: Optional[str], headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Return a live connection to ``browser_id``, connecting or recovering it.

        A failure marks the browser unhealthy and raises; it never touches the
        other browsers unless the Playwright driver itself is dead.
        """
        info = self.browsers.get(browser_id)
        if info is None:
            if not ws_url:
                raise KeyError(f"Browser '{browser_id}' not connected")
            try:
                return await self.connect_browser(browser_id, ws_url, headers=headers)
            except Exception as e:
                # A dead Node driver fails every plain connect; only then is
                # the driver restart worth it.
                if self.driver_alive():
                    self.mark_unhealthy(browser_id)
                    raise
                logger.warning(
                    f"Connect to '{browser_id}' failed and driver is dead, running full recovery: {e}"
                )
                return await self.recover_connection(browser_id, ws_url, headers=headers)

        drifted = bool(ws_url) and ws_url != info.ws_url
        if drifted or not info.browser.is_connected():
            if drifted:
                logger.info(
                    f"Browser '{browser_id}' ws_url drift ({info.ws_url} -> {ws_url}), reconnecting..."
                )
            else:
                logger.info(f"Browser '{browser_id}' connection is dead, auto-reconnecting...")
            return await self.recover_connection(browser_id, ws_url or info.ws_url, headers=headers)
        return info

    # ── recovery ────────────────────────────────────────────

    async def recover_connection(self, browser_id: str, ws_url: str, headers: Optional[dict] = None) -> BrowserInfo:
        """
        Recover a broken browser connection.

        Uses a lock so that concurrent requests don't all try to recover at
        the same time.  Two recovery levels are attempted:

        * Level 1 – disconnect the stale entry and reconnect via the
          **existing** Playwright driver.
        * Level 2 – only when the driver process is dead, restart it and
          reconnect every browser.

        ``headers`` defaults to the existing connection's headers (so BYO auth
        survives a reconnect) when not provided by the caller.
        """
        async with self._reconnect_lock:
            # Another request may have already recovered while we waited
            if browser_id in self.browsers:
                existing = self.browsers[browser_id]
                if headers is None:
                    headers = existing.headers
                # Only short-circuit if the URL also matches — ws_url drift
                # (browser pod restart with new IP/port) means we MUST reconnect.
                if existing.ws_url == ws_url and existing.browser.is_connected():
                    return existing

            # ── Level 1: simple reconnect with same Playwright driver ──
            try:
                info = await self._reconnect_same_driver(browser_id, ws_url, headers)
                self.mark_healthy(browser_id)
                return info
            except Exception as e:
                logger.warning(
                    f"Level-1 reconnect failed for '{browser_id}': {e}"
                )
                # With a live driver the fault is this browser (not ready,
                # offline, gone). Restarting the driver would drop every other
                # browser's connection and tabs, so fail this one alone.
                if self.driver_alive():
                    self.mark_unhealthy(browser_id)
                    raise

            # ── Level 2: restart Playwright driver entirely ──
            try:
                info = await self._restart_playwright_and_reconnect(browser_id, ws_url, headers)
            except Exception:
                self.mark_unhealthy(browser_id)
                raise
            self.mark_healthy(browser_id)
            return info

    async def _reconnect_same_driver(
        self, browser_id: str, ws_url: str, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Disconnect stale entry and open a fresh CDP connection."""
        try:
            await asyncio.wait_for(
                self.disconnect_browser(browser_id), timeout=10.0
            )
        except Exception as e:
            logger.warning(f"Error disconnecting '{browser_id}': {e}")
            self.browsers.pop(browser_id, None)

        browser = await self.playwright.chromium.connect_over_cdp(ws_url, headers=headers or None)
        context = (
            browser.contexts[0] if browser.contexts
            else await browser.new_context()
        )
        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id, headers=headers or {})
        self.browsers[browser_id] = info
        logger.info(f"Reconnected browser '{browser_id}' (same driver)")
        return info

    async def _restart_playwright_and_reconnect(
        self, browser_id: str, fresh_ws_url: Optional[str] = None, headers: Optional[dict] = None
    ) -> BrowserInfo:
        """Restart the Playwright driver process and reconnect every browser."""
        logger.warning("Restarting Playwright driver for full recovery")

        saved = {bid: (info.ws_url, info.headers) for bid, info in self.browsers.items()}
        if fresh_ws_url:
            saved[browser_id] = (fresh_ws_url, headers if headers is not None else saved.get(browser_id, (None, {}))[1])
        self.browsers.clear()

        old_pw = self.playwright
        self.playwright = None

        if old_pw is not None:
            try:
                await asyncio.wait_for(old_pw.stop(), timeout=5.0)
            except Exception:
                logger.warning(
                    "Old Playwright driver did not stop cleanly, force-killing"
                )
                self._kill_playwright_process()

        # Brief pause to let the OS reclaim sockets / pipes
        await asyncio.sleep(0.5)

        from patchright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self._track_playwright_pid()
        logger.info("Playwright driver restarted")

        new_info: Optional[BrowserInfo] = None
        for bid, (url, hdrs) in saved.items():
            try:
                browser = await asyncio.wait_for(
                    self.playwright.chromium.connect_over_cdp(url, headers=hdrs or None),
                    timeout=15.0,
                )
                context = (
                    browser.contexts[0] if browser.contexts
                    else await browser.new_context()
                )
                info = BrowserInfo(browser, context, ws_url=url, browser_id=bid, headers=hdrs or {})
                self.browsers[bid] = info
                if bid == browser_id:
                    new_info = info
                logger.info(f"Reconnected '{bid}' after Playwright restart")
            except Exception as e:
                logger.error(
                    f"Failed to reconnect '{bid}' after Playwright restart: {e}"
                )

        if new_info is None:
            raise RuntimeError(
                f"Failed to recover browser '{browser_id}' "
                "after Playwright restart"
            )
        return new_info

    # ── lifecycle ────────────────────────────────────────────

    async def cleanup_stale_pages(self) -> int:
        """Close session pages that are no longer usable.

        Patchright's Node driver leaks memory as dead/zombie pages accumulate
        (each holds driver-side references), eventually OOM-crashing it. We drop
        pages whose handle is closed or whose context is gone. Returns the count.
        """
        closed = 0
        for info in list(self.browsers.values()):
            for sid, page in list(info.pages.items()):
                dead = False
                try:
                    if page.is_closed():
                        dead = True
                    else:
                        _ = page.url  # touch — raises if the page/context is gone
                except Exception:
                    dead = True
                if dead:
                    info.pages.pop(sid, None)
                    try:
                        await page.close()
                    except Exception:
                        pass
                    closed += 1
        if closed:
            logger.info(f"Stale page cleanup: closed {closed} dead page(s)")
        return closed

    async def shutdown(self, timeout: float = 25.0):
        """Disconnect all browsers."""
        logger.info("Starting browser manager shutdown…")

        async def _shutdown():
            for bid in list(self.browsers.keys()):
                info = self.browsers[bid]
                for page in list(info.pages.values()):
                    try:
                        await page.close()
                    except Exception:
                        pass
                try:
                    await info.browser.close()
                except Exception:
                    pass
            self.browsers.clear()
            logger.info("All browsers disconnected")

        try:
            await asyncio.wait_for(_shutdown(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s, forcing cleanup")
            self.browsers.clear()


# Global singleton
browser_manager = BrowserManager()
