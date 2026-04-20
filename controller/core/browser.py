import asyncio
import logging
import os
import signal
from typing import Optional

from patchright.async_api import Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


class BrowserInfo:
    """Container for a connected browser, its context, and active pages."""

    def __init__(self, browser: Browser, context: BrowserContext, ws_url: str = "", browser_id: str = ""):
        self.browser = browser
        self.context = context
        self.ws_url = ws_url
        self.browser_id = browser_id
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
        self._reconnect_lock = asyncio.Lock()
        self._playwright_pid: Optional[int] = None

    async def start(self, playwright: Playwright):
        """Initialise with a Playwright instance. No auto-connections."""
        self.playwright = playwright
        self._track_playwright_pid()
        logger.info("Browser manager started (agnostic mode — waiting for registrations)")

    def _track_playwright_pid(self):
        try:
            if self.playwright and hasattr(self.playwright, '_impl_obj'):
                obj = self.playwright._impl_obj
                if hasattr(obj, '_browser'):
                    transport = getattr(obj._browser, '_transport', None)
                    if transport and hasattr(transport, '_proc'):
                        self._playwright_pid = transport._proc.pid
                        logger.info(f"Tracking Playwright driver PID: {self._playwright_pid}")
        except Exception:
            pass

    def _kill_playwright_process(self):
        if self._playwright_pid:
            try:
                os.kill(self._playwright_pid, signal.SIGKILL)
                logger.warning(f"Force-killed old Playwright driver PID {self._playwright_pid}")
            except (ProcessLookupError, PermissionError):
                pass
            self._playwright_pid = None

    # ── connect / disconnect ─────────────────────────────────

    async def connect_browser(self, browser_id: str, ws_url: str) -> BrowserInfo:
        """
        Connect to a remote browser over CDP.

        If ``browser_id`` is already connected **with the same URL** and the
        connection is still alive, the existing connection is returned.
        If the connection is dead (e.g. browser restarted), it auto-reconnects.
        If the URL differs, ``ValueError`` is raised — disconnect first.
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if browser_id in self.browsers:
            existing = self.browsers[browser_id]
            if existing.ws_url == ws_url and existing.browser.is_connected():
                logger.info(f"Browser '{browser_id}' already connected (idempotent)")
                return existing
            if existing.ws_url != ws_url:
                raise ValueError(
                    f"Browser '{browser_id}' already connected with a different URL. "
                    "Disconnect it first."
                )
            logger.info(f"Browser '{browser_id}' connection is dead, reconnecting...")
            await self.disconnect_browser(browser_id)

        logger.info(f"Connecting to browser '{browser_id}' via {ws_url}")
        browser = await self.playwright.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id)
        self.browsers[browser_id] = info
        logger.info(f"Connected to browser '{browser_id}'")
        return info

    async def disconnect_browser(self, browser_id: str) -> bool:
        """Disconnect a browser and close all its pages."""
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

    # ── lookup helpers ───────────────────────────────────────

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a connected browser by ID. Raises ``KeyError`` if not found."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser '{browser_id}' not connected")
        return self.browsers[browser_id]

    def first_browser_id(self) -> Optional[str]:
        """Return the ID of the first connected browser, or None."""
        return next(iter(self.browsers), None)

    # ── recovery ────────────────────────────────────────────

    async def recover_connection(self, browser_id: str, ws_url: str) -> BrowserInfo:
        """
        Recover a broken browser connection.

        Uses a lock so that concurrent requests don't all try to recover at
        the same time.  Two recovery levels are attempted:

        * Level 1 – disconnect the stale entry and reconnect via the
          **existing** Playwright driver.
        * Level 2 – if the driver pipe is broken, restart the entire
          Playwright driver process and reconnect every browser.
        """
        async with self._reconnect_lock:
            # Another request may have already recovered while we waited
            if browser_id in self.browsers:
                existing = self.browsers[browser_id]
                if existing.browser.is_connected():
                    try:
                        # Actually probe the connection (is_connected can lie)
                        _ = existing.browser.version
                        return existing
                    except Exception:
                        pass  # stale, proceed with recovery

            # ── Level 1: simple reconnect with same Playwright driver ──
            try:
                return await self._reconnect_same_driver(browser_id, ws_url)
            except Exception as e:
                logger.warning(
                    f"Level-1 reconnect failed for '{browser_id}': {e}"
                )

            # ── Level 2: restart Playwright driver entirely ──
            return await self._restart_playwright_and_reconnect(browser_id)

    async def _reconnect_same_driver(
        self, browser_id: str, ws_url: str
    ) -> BrowserInfo:
        """Disconnect stale entry and open a fresh CDP connection."""
        try:
            await asyncio.wait_for(
                self.disconnect_browser(browser_id), timeout=10.0
            )
        except Exception as e:
            logger.warning(f"Error disconnecting '{browser_id}': {e}")
            self.browsers.pop(browser_id, None)

        browser = await self.playwright.chromium.connect_over_cdp(ws_url)
        context = (
            browser.contexts[0] if browser.contexts
            else await browser.new_context()
        )
        info = BrowserInfo(browser, context, ws_url=ws_url, browser_id=browser_id)
        self.browsers[browser_id] = info
        logger.info(f"Reconnected browser '{browser_id}' (same driver)")
        return info

    async def _restart_playwright_and_reconnect(
        self, browser_id: str
    ) -> BrowserInfo:
        """Restart the Playwright driver process and reconnect every browser."""
        logger.warning("Restarting Playwright driver for full recovery")

        saved = {bid: info.ws_url for bid, info in self.browsers.items()}
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
        for bid, url in saved.items():
            try:
                browser = await asyncio.wait_for(
                    self.playwright.chromium.connect_over_cdp(url),
                    timeout=15.0,
                )
                context = (
                    browser.contexts[0] if browser.contexts
                    else await browser.new_context()
                )
                info = BrowserInfo(browser, context, ws_url=url, browser_id=bid)
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

    async def shutdown(self, timeout: float = 25.0):
        """Disconnect all browsers."""
        logger.info("Starting browser manager shutdown…")

        async def _shutdown():
            for bid in list(self.browsers.keys()):
                info = self.browsers[bid]
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

    async def cleanup_stale_pages(self):
        """Close pages that are no longer responsive to reclaim memory."""
        for bid, info in list(self.browsers.items()):
            stale_sessions = []
            for session_id, page in list(info.pages.items()):
                try:
                    _ = page.url
                except Exception:
                    stale_sessions.append(session_id)
            for session_id in stale_sessions:
                page = info.pages.pop(session_id, None)
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                    logger.info(f"Cleaned up stale page for session {session_id} in browser '{bid}'")


# Global singleton
browser_manager = BrowserManager()
