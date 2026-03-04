import asyncio
import logging
from typing import Optional
import httpx
import os

from patchright.async_api import Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_ID = "default"
# Get launcher URL from env, or default to livellm-browser container
LAUNCHER_URL = os.environ.get("LAUNCHER_URL", "http://livellm-browser:9000")
# We also need the hostname to connect via CDP
LAUNCHER_HOST = os.environ.get("LAUNCHER_HOST", "livellm-browser")

class BrowserInfo:
    """Container for browser, context, and its associated pages."""
    def __init__(self, browser: Browser, context: BrowserContext, profile_path: Optional[str] = None):
        self.browser = browser
        self.context = context
        self.profile_path = profile_path
        self.pages: dict[str, Page] = {}

class BrowserManager:
    """Manages connections to remote browser instances via CDP."""

    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browsers: dict[str, BrowserInfo] = {}
        self.http_client = httpx.AsyncClient(base_url=LAUNCHER_URL, timeout=30.0)

    async def start(self, playwright: Playwright):
        """Initialize with a Playwright instance and connect to the default browser."""
        self.playwright = playwright
        
        max_retries = 40
        for i in range(max_retries):
            try:
                # First check if the default browser is already up
                resp = await self.http_client.get("/browsers")
                resp.raise_for_status()
                browsers = resp.json()
                default_b = next((b for b in browsers if b["browser_id"] == DEFAULT_BROWSER_ID), None)
                
                if default_b:
                    await self._connect_to_existing(default_b)
                else:
                    await self.create_browser(profile_uid=DEFAULT_BROWSER_ID)
                
                logger.info("Remote browser manager started with default browser")
                return
            except httpx.ConnectError:
                logger.warning(f"Launcher not ready yet, retrying in 3 seconds ({i+1}/{max_retries})...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Failed to connect to launcher: {e}")
                raise
        raise RuntimeError("Failed to connect to browser launcher after multiple retries")

    async def _connect_to_existing(self, data: dict) -> tuple[str, BrowserInfo]:
        """Connect to a browser already running on the launcher."""
        browser_id = data["browser_id"]
        cdp_port = data["cdp_port"]
        ws_endpoint = data["ws_endpoint"]
        profile_path = data.get("profile_path")

        ws_url = f"ws://{LAUNCHER_HOST}:{cdp_port}{ws_endpoint}"
        logger.info(f"Connecting to CDP WebSocket: {ws_url}")
        
        browser = await self.playwright.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        browser_info = BrowserInfo(browser, context, profile_path)
        self.browsers[browser_id] = browser_info

        logger.info(f"Connected to remote browser '{browser_id}' on port {cdp_port}")
        return browser_id, browser_info

    async def create_browser(self, profile_uid: Optional[str] = None, proxy=None) -> tuple[str, BrowserInfo]:
        """Request a new browser from the launcher and connect to it over CDP."""
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if profile_uid and profile_uid in self.browsers:
            raise ValueError(f"Browser with id '{profile_uid}' already exists")

        payload = {}
        if profile_uid:
            payload["profile_uid"] = profile_uid
        if proxy:
            # handle proxy dict or BaseModel
            payload["proxy"] = proxy.model_dump() if hasattr(proxy, "model_dump") else proxy

        resp = await self.http_client.post("/browsers", json=payload)
        if resp.status_code != 200:
            raise ValueError(f"Failed to create browser on launcher: {resp.text}")

        return await self._connect_to_existing(resp.json())

    def get_browser(self, browser_id: str) -> BrowserInfo:
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")
        return self.browsers[browser_id]

    def get_default_browser(self) -> BrowserInfo:
        return self.browsers[DEFAULT_BROWSER_ID]

    def get_default_browser_id(self) -> str:
        return DEFAULT_BROWSER_ID

    async def close_browser(self, browser_id: str) -> bool:
        """Close remote browser and local connection."""
        if browser_id == DEFAULT_BROWSER_ID:
            raise ValueError("Cannot close the default browser")
        if browser_id not in self.browsers:
            return False

        # Close on launcher
        resp = await self.http_client.delete(f"/browsers/{browser_id}")
        if resp.status_code not in (200, 404):
            logger.warning(f"Failed to delete browser on launcher: {resp.text}")

        browser_info = self.browsers[browser_id]
        for page in list(browser_info.pages.values()):
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
        try:
            await browser_info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser connection: {e}")

        del self.browsers[browser_id]
        logger.info(f"Closed remote browser '{browser_id}'")
        return True

    async def shutdown(self, timeout: float = 25.0):
        """Disconnect all browsers."""
        logger.info("Starting browser manager shutdown...")
        for browser_id in list(self.browsers.keys()):
            info = self.browsers[browser_id]
            try:
                await info.browser.close()
            except Exception:
                pass
        self.browsers.clear()
        await self.http_client.aclose()
        logger.info("All browsers disconnected")

# Global singleton
browser_manager = BrowserManager()
