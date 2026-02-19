import asyncio
import os
import shutil
import uuid
import logging
from pathlib import Path
from typing import Optional

from patchright.async_api import Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("./profiles")
DEFAULT_BROWSER_ID = "default"


def cleanup_profile_locks(profile_path: Path):
    """Remove Chrome lock files from a profile directory to prevent startup errors."""
    if not profile_path.exists():
        return

    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_file = profile_path / lock_name
        if os.path.lexists(lock_file):
            try:
                if os.path.islink(lock_file):
                    os.unlink(lock_file)
                elif lock_file.is_dir():
                    shutil.rmtree(lock_file)
                else:
                    lock_file.unlink()
                logger.info(f"Removed lock file: {lock_file}")
            except Exception as e:
                logger.warning(f"Failed to remove lock file {lock_file}: {e}")


class BrowserInfo:
    """Container for browser, context, and its associated pages."""

    def __init__(self, browser: Browser, context: BrowserContext, profile_path: Optional[Path] = None):
        self.browser = browser
        self.context = context
        self.profile_path = profile_path
        self.pages: dict[str, Page] = {}


class BrowserManager:
    """Manages multiple browser instances with persistent and ephemeral profiles."""

    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browsers: dict[str, BrowserInfo] = {}

    async def start(self, playwright: Playwright):
        """Initialize with a Playwright instance and create the default browser."""
        self.playwright = playwright
        await self.create_browser(profile_uid=DEFAULT_BROWSER_ID)
        logger.info("Browser manager started with default browser")

    async def create_browser(self, profile_uid: Optional[str] = None, proxy=None) -> tuple[str, BrowserInfo]:
        """
        Create a new browser instance.

        Args:
            profile_uid: If provided, creates persistent profile in profiles/{uid}.
                         If not provided, creates an ephemeral browser.
            proxy: Optional proxy settings (ProxySettings model).

        Returns:
            Tuple of (browser_id, BrowserInfo).
        """
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        if profile_uid:
            browser_id = profile_uid
            profile_path = PROFILES_DIR / profile_uid
            cleanup_profile_locks(profile_path)
            is_persistent = True
        else:
            browser_id = str(uuid.uuid4())
            profile_path = None
            is_persistent = False

        if browser_id in self.browsers:
            raise ValueError(f"Browser with id '{browser_id}' already exists")

        # Build proxy config
        proxy_config = None
        if proxy:
            proxy_config = {"server": proxy.server}
            if proxy.username:
                proxy_config["username"] = proxy.username
            if proxy.password:
                proxy_config["password"] = proxy.password
            if proxy.bypass:
                proxy_config["bypass"] = proxy.bypass
            logger.info(f"Browser '{browser_id}' configured with proxy: {proxy.server}")

        browser = None
        context = None

        if is_persistent:
            launch_kwargs = {
                "user_data_dir": str(profile_path),
                "headless": False,
                "channel": "chrome",
                "no_viewport": True,
                "args": ["--start-maximized"],
            }
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
            context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            browser = context.browser
        else:
            launch_kwargs = {
                "headless": False,
                "channel": "chrome",
                "args": ["--start-maximized"],
            }
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config
            browser = await self.playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(no_viewport=True)

        if browser is None and context:
            browser = context.browser
        if browser is None:
            raise RuntimeError(f"Failed to get browser object for {browser_id}")

        browser_info = BrowserInfo(browser, context, profile_path)
        self.browsers[browser_id] = browser_info

        kind = "persistent" if is_persistent else "ephemeral"
        logger.info(f"Created {kind} browser '{browser_id}'" + (f" with profile at {profile_path}" if is_persistent else ""))
        return browser_id, browser_info

    def get_browser(self, browser_id: str) -> BrowserInfo:
        """Get a browser by its ID. Raises KeyError if not found."""
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")
        return self.browsers[browser_id]

    def get_default_browser(self) -> BrowserInfo:
        return self.browsers[DEFAULT_BROWSER_ID]

    def get_default_browser_id(self) -> str:
        return DEFAULT_BROWSER_ID

    async def close_browser(self, browser_id: str) -> bool:
        """Close and remove a browser instance. Cannot close the default browser."""
        if browser_id == DEFAULT_BROWSER_ID:
            raise ValueError("Cannot close the default browser")
        if browser_id not in self.browsers:
            return False

        browser_info = self.browsers[browser_id]
        for page in browser_info.pages.values():
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing page: {e}")
        try:
            await browser_info.context.close()
        except Exception as e:
            logger.warning(f"Error closing context: {e}")
        try:
            await browser_info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser: {e}")

        del self.browsers[browser_id]
        logger.info(f"Closed browser '{browser_id}'")
        return True

    async def shutdown(self, timeout: float = 25.0):
        """Close all browsers with timeout protection."""
        logger.info("Starting browser shutdown...")

        async def _shutdown_task():
            for browser_id in list(self.browsers.keys()):
                info = self.browsers[browser_id]
                for page in info.pages.values():
                    try:
                        await asyncio.wait_for(page.close(), timeout=2.0)
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"Error closing page: {e}")
                try:
                    await asyncio.wait_for(info.context.close(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"Error closing context for browser {browser_id}: {e}")
                try:
                    await asyncio.wait_for(info.browser.close(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"Error closing browser {browser_id}: {e}")
            self.browsers.clear()
            logger.info("All browsers closed")

        try:
            await asyncio.wait_for(_shutdown_task(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Shutdown timed out after {timeout}s, forcing cleanup")
            self.browsers.clear()


# Global singleton
browser_manager = BrowserManager()
