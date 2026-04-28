import asyncio
import os
import shutil
import time
import uuid
import logging
import socket
import httpx
import json
import zipfile
import io
from pathlib import Path
from typing import Optional, Any, List

from patchright.async_api import Playwright, Browser, BrowserContext, Page
from core.cdp_proxy import CDPProxy
from core.const import PROFILES_DIR, EXTENSIONS_CACHE_DIR, DEFAULT_BROWSER_ID, STABLE_WS_PREFIX
from core.redis_state import redis_browser_state

logger = logging.getLogger(__name__)


async def download_extension(extension_id: str) -> Path:
    """Download a Chrome extension CRX from the Web Store and unpack it to a cache directory."""
    EXTENSIONS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ext_path = EXTENSIONS_CACHE_DIR / extension_id

    manifest_file = ext_path / "manifest.json"
    if ext_path.exists() and manifest_file.exists():
        return ext_path

    logger.info(f"Downloading extension {extension_id}...")
    url = (
        "https://clients2.google.com/service/update2/crx"
        "?response=redirect&os=win&arch=x86-64&os_arch=x86-64&nacl_arch=x86-64"
        "&prod=chromecrx&prodchannel=&prodversion=114.0.5735.199&lang=en-US&acceptformat=crx3"
        f"&x=id%3D{extension_id}%26installsource%3Dondemand%26uc"
    )

    async with httpx.AsyncClient() as client:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        }
        response = await client.get(url, headers=headers, follow_redirects=True)
        response.raise_for_status()

        content = response.content
        if not content:
            raise ValueError(f"Empty response for extension {extension_id}")

        zip_start = content.find(b'PK\x03\x04')
        if zip_start == -1:
            raise ValueError(f"Not a valid CRX/ZIP file for extension {extension_id}")

        ext_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(content[zip_start:])) as zf:
            zf.extractall(ext_path)

        # Chrome rejects side-loaded extensions that contain _metadata
        metadata_dir = ext_path / "_metadata"
        if metadata_dir.exists():
            shutil.rmtree(metadata_dir)

    logger.info(f"Extension {extension_id} cached at {ext_path}")
    return ext_path


def inject_extension_into_profile(extension_id: str, cache_path: Path, profile_path: Path):
    """
    Copy an unpacked extension into a Chrome profile's Extensions folder
    and register it in the Preferences file so Chrome actually loads it.
    """
    with open(cache_path / "manifest.json", "r", encoding="utf-8") as f:
        manifest = json.load(f)
    version = manifest.get("version", "1.0.0")

    default_dir = profile_path / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy extension files into Default/Extensions/<id>/<version>_0/
    target_dir = default_dir / "Extensions" / extension_id / f"{version}_0"
    if not target_dir.exists():
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cache_path, target_dir)
        logger.info(f"Copied extension {extension_id} v{version} into profile")

    # 2. Register the extension in Default/Preferences
    prefs_path = default_dir / "Preferences"
    prefs = {}
    if prefs_path.exists():
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, IOError):
            prefs = {}

    ext_settings = prefs.setdefault("extensions", {}).setdefault("settings", {})

    permissions = manifest.get("permissions", [])
    host_permissions = manifest.get("host_permissions", [])
    optional_permissions = manifest.get("optional_permissions", [])

    # Extract scriptable hosts from content_scripts[].matches
    scriptable_hosts = []
    for cs in manifest.get("content_scripts", []):
        scriptable_hosts.extend(cs.get("matches", []))
    scriptable_hosts = sorted(set(scriptable_hosts))

    # Chrome timestamps are microseconds since Windows epoch (Jan 1, 1601)
    chrome_timestamp = str(int((time.time() + 11644473600) * 1_000_000))

    # creation_flags bitmask: 4 = FROM_WEBSTORE
    creation_flags = 4

    ext_settings[extension_id] = {
        "active_permissions": {
            "api": permissions,
            "explicit_host": host_permissions,
            "manifest_permissions": [],
            "scriptable_host": scriptable_hosts
        },
        "commands": {},
        "content_settings": [],
        "creation_flags": creation_flags,
        "from_webstore": True,
        "granted_permissions": {
            "api": permissions + optional_permissions,
            "explicit_host": host_permissions,
            "manifest_permissions": [],
            "scriptable_host": scriptable_hosts
        },
        "incognito_content_settings": [],
        "incognito_preferences": {},
        "install_time": chrome_timestamp,
        "location": 1,
        "manifest": manifest,
        "path": f"{extension_id}/{version}_0",
        "state": 1,
        "was_installed_by_default": False,
        "was_installed_by_oem": False
    }

    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False)
    logger.info(f"Registered extension {extension_id} in Preferences")

    # 3. Remove Secure Preferences so Chrome regenerates it
    #    (otherwise HMAC mismatch causes Chrome to strip our changes)
    secure_prefs = default_dir / "Secure Preferences"
    if secure_prefs.exists():
        secure_prefs.unlink()
        logger.info("Removed Secure Preferences (will be regenerated by Chrome)")


def remove_extension_from_profile(extension_id: str, profile_path: Path):
    """Remove an extension from a Chrome profile's Extensions folder and Preferences."""
    default_dir = profile_path / "Default"

    ext_dir = default_dir / "Extensions" / extension_id
    if ext_dir.exists():
        shutil.rmtree(ext_dir)
        logger.info(f"Removed extension files for {extension_id}")

    prefs_path = default_dir / "Preferences"
    if prefs_path.exists():
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            settings = prefs.get("extensions", {}).get("settings", {})
            if extension_id in settings:
                del settings[extension_id]
                with open(prefs_path, "w", encoding="utf-8") as f:
                    json.dump(prefs, f, ensure_ascii=False)
                logger.info(f"Unregistered extension {extension_id} from Preferences")
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not update Preferences: {e}")

    secure_prefs = default_dir / "Secure Preferences"
    if secure_prefs.exists():
        secure_prefs.unlink()


def set_extension_enabled(extension_id: str, profile_path: Path, enabled: bool):
    """Toggle an extension's enabled state in Preferences (state: 1=enabled, 0=disabled)."""
    default_dir = profile_path / "Default"
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        raise FileNotFoundError(f"Preferences file not found in {default_dir}")

    with open(prefs_path, "r", encoding="utf-8") as f:
        prefs = json.load(f)

    settings = prefs.get("extensions", {}).get("settings", {})
    if extension_id not in settings:
        raise KeyError(f"Extension {extension_id} not found in Preferences")

    settings[extension_id]["state"] = 1 if enabled else 0

    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False)

    secure_prefs = default_dir / "Secure Preferences"
    if secure_prefs.exists():
        secure_prefs.unlink()

    state_str = "enabled" if enabled else "disabled"
    logger.info(f"Extension {extension_id} {state_str} in Preferences")


def list_profile_extensions(profile_path: Path) -> list[dict]:
    """List extensions installed in a profile, including their enabled/disabled state."""
    default_dir = profile_path / "Default"
    ext_base = default_dir / "Extensions"
    results = []
    if not ext_base.exists():
        return results

    # Read Preferences to get state info
    prefs_path = default_dir / "Preferences"
    ext_settings = {}
    if prefs_path.exists():
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            ext_settings = prefs.get("extensions", {}).get("settings", {})
        except (json.JSONDecodeError, IOError):
            pass

    for ext_dir in ext_base.iterdir():
        if not ext_dir.is_dir():
            continue
        extension_id = ext_dir.name
        for version_dir in ext_dir.iterdir():
            if not version_dir.is_dir():
                continue
            manifest_path = version_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    state = ext_settings.get(extension_id, {}).get("state", 1)
                    results.append({
                        "id": extension_id,
                        "name": manifest.get("name", ""),
                        "version": manifest.get("version", ""),
                        "enabled": state == 1,
                    })
                except (json.JSONDecodeError, IOError):
                    results.append({"id": extension_id, "name": "", "version": "", "enabled": False})
    return results


def get_free_port():
    """Return a free TCP port, keeping the socket held until the caller uses it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 0))
    port = s.getsockname()[1]
    # Keep the socket open to prevent TOCTOU races.  The caller should pass
    # the socket (or close it right before binding) so the port isn't stolen.
    return port, s


async def wait_for_chrome_cdp(chrome_port: int, timeout: float = 15.0) -> str:
    """Poll Chrome's /json/version endpoint until it responds.

    Returns the ``webSocketDebuggerUrl`` path (e.g. ``/devtools/browser/…``),
    or an empty string if Chrome never became ready.
    """
    import httpx
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{chrome_port}/json/version",
                    timeout=3.0,
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                full_ws_url: str = data.get("webSocketDebuggerUrl", "")
                if full_ws_url:
                    ws_endpoint = "/" + full_ws_url.replace("ws://", "").split("/", 1)[1]
                    logger.info(f"Chrome CDP ready on :{chrome_port} (ws: {ws_endpoint})")
                    return ws_endpoint
        except Exception:
            await asyncio.sleep(0.5)
    logger.warning(f"Chrome CDP on :{chrome_port} did not become ready within {timeout}s")
    return ""


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
    def __init__(self, browser: Browser, context: BrowserContext, proxy_port: int, ws_endpoint: str, cdp_proxy: CDPProxy, profile_path: Optional[Path] = None, is_ephemeral: bool = False, proxy_config: Optional[dict] = None):
        self.browser = browser
        self.context = context
        self.proxy_port = proxy_port
        self.ws_endpoint = ws_endpoint
        self.cdp_proxy = cdp_proxy
        self.profile_path = profile_path
        self.is_ephemeral = is_ephemeral
        self.proxy_config = proxy_config


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

    async def create_browser(self, profile_uid: Optional[str] = None, proxy=None, extensions: Optional[List[str]] = None, cookies: Optional[List[dict]] = None) -> tuple[str, LocalBrowserInfo]:
        """Create a new browser instance, optionally with extensions and cookies pre-loaded."""
        if not self.playwright:
            raise RuntimeError("Browser manager not started")

        is_ephemeral = False
        if profile_uid:
            browser_id = profile_uid
            profile_path = PROFILES_DIR / profile_uid
            cleanup_profile_locks(profile_path)
            is_persistent = True
        else:
            browser_id = str(uuid.uuid4())
            is_ephemeral = True
            if extensions:
                profile_path = PROFILES_DIR / browser_id
                is_persistent = True
            else:
                profile_path = None
                is_persistent = False

        if browser_id in self.browsers:
            raise ValueError(f"Browser with id '{browser_id}' already exists")

        # Download and inject extensions into the profile BEFORE Chrome launches
        if extensions and profile_path:
            profile_path.mkdir(parents=True, exist_ok=True)
            for ext_id in extensions:
                try:
                    cache_path = await download_extension(ext_id)
                    inject_extension_into_profile(ext_id, cache_path, profile_path)
                except Exception as e:
                    logger.error(f"Failed to inject extension {ext_id}: {e}")

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

        chrome_port, chrome_sock = get_free_port()
        proxy_port, proxy_sock = get_free_port()

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

        # Release the held sockets — Chrome has now bound to chrome_port, and
        # we're about to bind proxy_port for the CDP proxy.
        chrome_sock.close()
        proxy_sock.close()

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

        # Wait for Chrome's CDP port to become fully ready (with retries)
        ws_endpoint = await wait_for_chrome_cdp(chrome_port, timeout=15.0)

        if not ws_endpoint:
            logger.warning(f"Could not retrieve WS endpoint from Chrome on port {chrome_port}")

        cdp_proxy = CDPProxy(bind_port=proxy_port, target_port=chrome_port, ws_endpoint=ws_endpoint)
        await cdp_proxy.start()

        if cookies:
            try:
                await context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} cookies into browser '{browser_id}'")
            except Exception as e:
                logger.error(f"Failed to load cookies into browser '{browser_id}': {e}")

        browser_info = LocalBrowserInfo(browser, context, proxy_port, ws_endpoint, cdp_proxy, profile_path, is_ephemeral, proxy_config)
        self.browsers[browser_id] = browser_info

        kind = "persistent" if is_persistent else "ephemeral"
        logger.info(f"Created {kind} browser '{browser_id}' on proxy port {proxy_port}" + (f" with profile at {profile_path}" if is_persistent else ""))

        await redis_browser_state.register_browser(browser_id, proxy_port, cdp_port=chrome_port, extensions=extensions or [])

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

        if browser_info.is_ephemeral and browser_info.profile_path and browser_info.profile_path.exists():
            try:
                shutil.rmtree(browser_info.profile_path)
                logger.info(f"Cleaned up ephemeral profile at {browser_info.profile_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up ephemeral profile: {e}")

        del self.browsers[browser_id]
        logger.info(f"Closed browser '{browser_id}'")

        # Unregister from Redis
        await redis_browser_state.unregister_browser(browser_id)

        return True

    async def restart_browser(self, browser_id: str, inject_extensions: Optional[List[tuple]] = None, remove_extensions: Optional[List[str]] = None, toggle_extensions: Optional[List[tuple]] = None, proxy_config: Optional[dict] = None, clear_proxy: bool = False) -> LocalBrowserInfo:
        """
        Close a browser and relaunch it with the same profile.
        The CDP proxy port stays the same — only the Chrome target is updated.
        inject_extensions: list of (extension_id, cache_path) tuples to add AFTER Chrome exits.
        remove_extensions: list of extension_ids to remove AFTER Chrome exits.
        toggle_extensions: list of (extension_id, enabled) tuples to enable/disable AFTER Chrome exits.
        proxy_config: when set, replaces the upstream HTTP proxy used by the relaunched Chrome.
        clear_proxy: when True, drops any existing proxy config (proxy_config is ignored).
        """
        if browser_id not in self.browsers:
            raise KeyError(f"Browser with id '{browser_id}' not found")

        old_info = self.browsers[browser_id]
        profile_path = old_info.profile_path
        if clear_proxy:
            proxy_config = None
        elif proxy_config is None:
            proxy_config = old_info.proxy_config
        is_ephemeral = old_info.is_ephemeral
        cdp_proxy = old_info.cdp_proxy
        proxy_port = old_info.proxy_port

        if not profile_path:
            raise ValueError("Cannot restart an ephemeral browser without a profile.")

        # Shut down Chrome but keep the CDP proxy alive
        try:
            await old_info.context.close()
        except Exception as e:
            logger.warning(f"Error closing context during restart: {e}")
        try:
            if old_info.browser:
                await old_info.browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser during restart: {e}")

        del self.browsers[browser_id]
        logger.info(f"Stopped browser '{browser_id}' for restart (proxy on :{proxy_port} kept alive)")

        # Modify extensions AFTER Chrome has exited (so it can't overwrite Preferences)
        if inject_extensions:
            for ext_id, cache_path in inject_extensions:
                inject_extension_into_profile(ext_id, cache_path, profile_path)
        if remove_extensions:
            for ext_id in remove_extensions:
                remove_extension_from_profile(ext_id, profile_path)
        if toggle_extensions:
            for ext_id, enabled in toggle_extensions:
                set_extension_enabled(ext_id, profile_path, enabled)

        cleanup_profile_locks(profile_path)

        chrome_port, chrome_sock = get_free_port()

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

        # Release the held socket — Chrome has now bound to chrome_port
        chrome_sock.close()

        launch_kwargs["user_data_dir"] = str(profile_path)
        launch_kwargs["no_viewport"] = True
        context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        browser = context.browser
        if browser is None:
            browser = context.browser
        if browser is None:
            raise RuntimeError(f"Failed to get browser object for {browser_id}")

        # Wait for Chrome's CDP port to become fully ready (with retries)
        ws_endpoint = await wait_for_chrome_cdp(chrome_port, timeout=15.0)

        if not ws_endpoint:
            logger.warning(f"Could not retrieve WS endpoint from Chrome on port {chrome_port}")

        # Retarget the existing proxy to the new Chrome instance
        cdp_proxy.retarget(chrome_port, ws_endpoint)

        browser_info = LocalBrowserInfo(browser, context, proxy_port, ws_endpoint, cdp_proxy, profile_path, is_ephemeral, proxy_config)
        self.browsers[browser_id] = browser_info

        logger.info(f"Restarted browser '{browser_id}' (proxy :{proxy_port} -> Chrome :{chrome_port})")

        # Re-register in Redis (WS URL may have changed)
        await redis_browser_state.register_browser(browser_id, proxy_port)

        return browser_info

    async def shutdown(self, timeout: float = 25.0):
        logger.info("Starting browser shutdown...")

        # Unregister all browsers from Redis
        await redis_browser_state.unregister_all()

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
