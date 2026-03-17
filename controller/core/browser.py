import asyncio
import logging
from typing import Optional

from patchright.async_api import Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


class BrowserInfo:
    """Container for a connected browser, its context, and active pages."""

    def __init__(self, browser: Browser, context: BrowserContext, ws_url: str = ""):
        self.browser = browser
        self.context = context
        self.ws_url = ws_url
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

    async def start(self, playwright: Playwright):
        """Initialise with a Playwright instance. No auto-connections."""
        self.playwright = playwright
        logger.info("Browser manager started (agnostic mode — waiting for registrations)")

    # ── connect / disconnect ─────────────────────────────────

    async def connect_browser(self, browser_id: str, ws_url: str) -> BrowserInfo:
        """
        Connect to a remote browser over CDP.

        If ``browser_id`` is already connected **with the same URL**, the
        existing connection is returned (idempotent).  If the URL differs,
        ``ValueError`` is raised — disconnect first.
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if browser_id in self.browsers:
            existing = self.browsers[browser_id]
            if existing.ws_url == ws_url:
                logger.info(f"Browser '{browser_id}' already connected (idempotent)")
                return existing
            raise ValueError(
                f"Browser '{browser_id}' already connected with a different URL. "
                "Disconnect it first."
            )

        logger.info(f"Connecting to browser '{browser_id}' via {ws_url}")
        browser = await self.playwright.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        info = BrowserInfo(browser, context, ws_url=ws_url)
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


# Global singleton
browser_manager = BrowserManager()
