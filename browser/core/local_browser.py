import asyncio
import os
import shutil
import uuid
import logging
import socket
# import urllib.request
import httpx
import json
from pathlib import Path
from typing import Optional, Any

from patchright.async_api import Playwright, Browser, BrowserContext, Page
from core.cdp_proxy import CDPProxy

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("/home/headless/Desktop/app/profiles")
DEFAULT_BROWSER_ID = "default"

def get_free_port():
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

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

class LocalBrowserInfo:
    """Container for browser, context, and its associated pages."""
    def __init__(self, browser: Browser, context: BrowserContext, proxy_port: int, ws_endpoint: str, cdp_proxy: CDPProxy, profile_path: Optional[Path] = None):
        self.browser = browser
        self.context = context
        self.proxy_port = proxy_port
        self.ws_endpoint = ws_endpoint
        self.cdp_proxy = cdp_proxy
        self.profile_path = profile_path

class LocalBrowserManager:
    """Manages multiple browser instances with persistent and ephemeral profiles."""

    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browsers: dict[str, LocalBrowserInfo] = {}

    async def start(self, playwright: Playwright):
        """Initialize with a Playwright instance and create the default browser."""
        self.playwright = playwright
        await self.create_browser(profile_uid=DEFAULT_BROWSER_ID)
        logger.info("Browser manager started with default browser")

    async def create_browser(self, profile_uid: Optional[str] = None, proxy=None) -> tuple[str, LocalBrowserInfo]:
        """
        Create a new browser instance.
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

        chrome_port = get_free_port()
        proxy_port = get_free_port()

        launch_kwargs = {
            "headless": False,
            "channel": "chrome",
            "args": [
                "--start-maximized",
                "--ignore-gpu-blocklist",
                "--enable-webgl",
                "--enable-gpu",
                f"--remote-debugging-port={chrome_port}",
                "--remote-allow-origins=*",
            ],
        }
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        if is_persistent:
            launch_kwargs["user_data_dir"] = str(profile_path)
            launch_kwargs["no_viewport"] = True
            context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            browser = context.browser
        else:
            browser = await self.playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(no_viewport=True)

        if browser is None and context:
            browser = context.browser
        if browser is None:
            raise RuntimeError(f"Failed to get browser object for {browser_id}")

        ws_endpoint = ""
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"http://127.0.0.1:{chrome_port}/json/version", timeout=10.0)
                response.raise_for_status()
                data: dict[str, Any] = response.json()
                full_ws_url: str = data.get("webSocketDebuggerUrl", "")
                if full_ws_url:
                    #   "webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/c72f5f19-4b99-4607-bee7-0ec9805a018c"
                    # Extract the path (e.g., /devtools/browser/abc-def)
                    ws_endpoint = "/" + full_ws_url.replace("ws://", "").split("/", 1)[1]
            except Exception:
                pass

        if not ws_endpoint:
            logger.warning(f"Could not retrieve WS endpoint from Chrome on port {chrome_port}")

        cdp_proxy = CDPProxy(bind_port=proxy_port, target_port=chrome_port)
        await cdp_proxy.start()

        browser_info = LocalBrowserInfo(browser, context, proxy_port, ws_endpoint, cdp_proxy, profile_path)
        self.browsers[browser_id] = browser_info

        kind = "persistent" if is_persistent else "ephemeral"
        logger.info(f"Created {kind} browser '{browser_id}' on proxy port {proxy_port}" + (f" with profile at {profile_path}" if is_persistent else ""))
        return browser_id, browser_info

    def get_browser(self, browser_id: str) -> LocalBrowserInfo:
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")
        return self.browsers[browser_id]

    async def close_browser(self, browser_id: str) -> bool:
        if browser_id == DEFAULT_BROWSER_ID:
            raise ValueError("Cannot close the default browser")
        if browser_id not in self.browsers:
            return False

        browser_info = self.browsers[browser_id]
        
        try:
            await browser_info.cdp_proxy.stop()
        except Exception as e:
            logger.warning(f"Error stopping CDP proxy: {e}")
            
        try:
            await browser_info.context.close()
        except Exception as e:
            logger.warning(f"Error closing context: {e}")
        try:
            if browser_info.browser:
                await browser_info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser: {e}")

        del self.browsers[browser_id]
        logger.info(f"Closed browser '{browser_id}'")
        return True

    async def shutdown(self, timeout: float = 25.0):
        logger.info("Starting browser shutdown...")
        async def _shutdown_task():
            for browser_id in list(self.browsers.keys()):
                info = self.browsers[browser_id]
                try:
                    await info.cdp_proxy.stop()
                except Exception as e:
                    logger.warning(f"Error stopping proxy for browser {browser_id}: {e}")
                    
                try:
                    await asyncio.wait_for(info.context.close(), timeout=5.0)
                except (asyncio.TimeoutError, Exception) as e:
                    logger.warning(f"Error closing context for browser {browser_id}: {e}")
                try:
                    if info.browser:
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

local_browser_manager = LocalBrowserManager()
