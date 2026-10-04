import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
import uvicorn
from patchright.async_api import async_playwright

from core.const import PROFILES_DIR, DEFAULT_BROWSER_ID, STABLE_WS_PREFIX, IMAGE_VERSION
from core.launch_prep import platform_proxy_config
from core.local_browser import (
    local_browser_manager,
    cleanup_profile_locks, download_extension, list_profile_extensions
)


class ProxySettings(BaseModel):
    server: str
    username: Optional[str] = None
    password: Optional[str] = None
    bypass: Optional[str] = None

class CreateBrowserRequest(BaseModel):
    # Joined to PROFILES_DIR: a plain name only, never a path.
    profile_uid: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    proxy: Optional[ProxySettings] = None
    extensions: Optional[list[str]] = None
    cookies: Optional[list[dict]] = None

class BrowserResponse(BaseModel):
    browser_id: str
    cdp_port: int
    ws_endpoint: str
    ws_stable_endpoint: str
    profile_path: Optional[str] = None

class ExtensionsRequest(BaseModel):
    extensions: list[str]

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)

def _default_browser_config_from_env():
    """Build the primary browser's startup config from environment variables.

    In the managed platform the operator passes desired state declaratively:
    a browser's extensions, proxy, and a mounted cookies file are set on the pod, so the default
    browser is created with them at boot. All are optional.
    """
    extensions = None
    exts_raw = os.environ.get("BROWSER_EXTENSIONS", "").strip()
    if exts_raw:
        try:
            parsed = json.loads(exts_raw)
            extensions = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            logger.warning("BROWSER_EXTENSIONS is not a JSON list; ignoring")

    # The platform proxy (bypass entries ignored: each would go out directly).
    proxy = None
    cfg = platform_proxy_config()
    if cfg:
        proxy = ProxySettings(**cfg)

    cookies = None
    cookies_file = os.environ.get("BROWSER_COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        try:
            with open(cookies_file) as f:
                loaded = json.load(f)
            cookies = loaded if isinstance(loaded, list) else None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read cookies file {cookies_file}: {e}")

    return extensions, proxy, cookies


async def _browser_watchdog():
    """Relaunch the default browser if the user closes it (or it crashes).

    One pod = one browser; if the Chromium window is closed via noVNC the
    browser disconnects but the in-pod CDP proxy keeps its fixed port. We detect
    the dead connection and restart_browser() (which retargets the same proxy),
    so the browser self-heals in-place without a pod restart.

    The Patchright driver itself can also die (the kernel OOM-kills the Node
    process): then every playwright object is a corpse — even is_connected()
    raises — and only a full recover_driver() helps, not a Chrome relaunch.
    """
    relaunch_failures = 0
    while True:
        await asyncio.sleep(10)
        try:
            # A dead driver takes every browser with it — heal it first.
            if not local_browser_manager.driver_alive():
                logger.warning("Patchright driver is dead — recovering it")
                await local_browser_manager.recover_driver()
                continue

            info = local_browser_manager.browsers.get(DEFAULT_BROWSER_ID)
            if info is None or local_browser_manager.restarting(DEFAULT_BROWSER_ID):
                continue
            if local_browser_manager.paused(DEFAULT_BROWSER_ID):
                continue  # closed on purpose; the pause resumes it

            try:
                browser_up = info.browser is not None and info.browser.is_connected()
            except Exception as e:
                logger.warning(f"Browser connection check failed: {e}")
                browser_up = False

            if not browser_up:
                logger.warning("Default browser is down — relaunching")
                try:
                    await local_browser_manager.restart_browser(DEFAULT_BROWSER_ID)
                    logger.info("Default browser relaunched")
                    relaunch_failures = 0
                except Exception as e:
                    # One failure may be transient — retry on the next tick.
                    # Repeated failures mean the driver is broken in a way
                    # driver_alive() can't see: force-replace it (which also
                    # costs every other browser in the pod, so not on the
                    # first strike).
                    relaunch_failures += 1
                    if relaunch_failures >= 2:
                        logger.warning(f"Relaunch failed again ({e}) — force-recovering the driver")
                        await local_browser_manager.recover_driver(force=True)
                        relaunch_failures = 0
                    else:
                        logger.warning(f"Relaunch failed ({e}); will retry")
        except Exception as e:
            logger.warning(f"Browser watchdog error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    default_profile = PROFILES_DIR / DEFAULT_BROWSER_ID
    cleanup_profile_locks(default_profile)
    default_profile.mkdir(parents=True, exist_ok=True)

    extensions, proxy, cookies = _default_browser_config_from_env()

    playwright = await async_playwright().start()
    await local_browser_manager.start(
        playwright, extensions=extensions, proxy=proxy, cookies=cookies
    )

    watchdog = asyncio.create_task(_browser_watchdog())

    yield

    watchdog.cancel()
    try:
        await watchdog
    except asyncio.CancelledError:
        pass
    logger.info("Application shutting down, cleaning up resources...")
    try:
        await local_browser_manager.shutdown(timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    # After a driver recovery the `playwright` captured above is a corpse —
    # stop whatever driver the manager currently owns.
    driver = local_browser_manager.playwright
    try:
        if driver:
            await asyncio.wait_for(driver.stop(), timeout=5.0)
    except Exception as e:
        logger.warning(f"Error stopping playwright: {e}")
    logger.info("Shutdown complete")

app = FastAPI(title="Browser Launcher API", lifespan=lifespan)


def _default_ws_info():
    """The single browser's stable CDP websocket (one pod = one browser)."""
    info = local_browser_manager.browsers.get(DEFAULT_BROWSER_ID)
    if not info:
        return None
    return {
        "browser_id": DEFAULT_BROWSER_ID,
        "cdp_port": info.proxy_port,
        "ws_endpoint": info.ws_endpoint,
        "ws_stable_endpoint": f"{STABLE_WS_PREFIX}/{DEFAULT_BROWSER_ID}",
    }


@app.get("/")
async def root():
    """Single discovery endpoint — returns this pod's browser and its stable CDP
    websocket. One pod = one browser, so no per-browser routing is needed."""
    ws = _default_ws_info()
    if not ws:
        return Response(status_code=503, content="No browser")
    return ws


@app.get("/health")
async def health():
    # A deliberate pause (the profile is being copied) is healthy for at most
    # its own bound: without this the liveness probe would kill the pod
    # mid-copy.
    if local_browser_manager.paused(DEFAULT_BROWSER_ID):
        return {"status": "paused"}
    # In-pod recovery (watchdog, ≤10s detection) usually beats the liveness
    # probe (3×10s consecutive failures); when it doesn't, the pod restart is
    # the correct backstop.
    if not local_browser_manager.driver_alive():
        return Response(status_code=503, content="Patchright driver is not running")
    ws = _default_ws_info()
    if not ws:
        return Response(status_code=503, content="No browser")
    info = local_browser_manager.browsers.get(DEFAULT_BROWSER_ID)
    try:
        if not info.browser.is_connected():
            return Response(status_code=503, content="Browser disconnected")
    except Exception as e:
        return Response(status_code=503, content=f"Browser error: {e}")
    return {"status": "ok", **ws}

# ── Pod-local control (the control sidecar only) ──
#
# Loopback only: the public CDP host reaches this port from Traefik's pod IP.
# The header forces a CORS preflight, which this app never answers, so a page
# in Chrome (also loopback) can't send one either.

LOCAL_HEADER = "x-livellm-keeper"
MAX_PAUSE_SECONDS = 600


def _require_local(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1") or request.headers.get(LOCAL_HEADER) != "1":
        raise HTTPException(status_code=403, detail="forbidden")


class PauseRequest(BaseModel):
    maxSeconds: float = Field(..., gt=0, le=MAX_PAUSE_SECONDS)


def _chrome_pid(chrome_port: Optional[int], proc_root: str = "/proc") -> Optional[int]:
    """The Chrome browser process (not a renderer) on this debugging port."""
    if not chrome_port:
        return None
    want = f"--remote-debugging-port={chrome_port}".encode()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if want in argv and not any(a.startswith(b"--type=") for a in argv):
            return int(name)
    return None


@app.post("/browsers/default/pause")
async def pause_default(body: PauseRequest, request: Request) -> dict:
    _require_local(request)
    try:
        await local_browser_manager.pause_browser(DEFAULT_BROWSER_ID, body.maxSeconds)
    except KeyError:
        raise HTTPException(status_code=404, detail="No browser")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Chrome did not close")
    return {"status": "paused", "maxSeconds": body.maxSeconds}


@app.post("/browsers/default/resume")
async def resume_default(request: Request) -> dict:
    _require_local(request)
    try:
        await local_browser_manager.resume_browser(DEFAULT_BROWSER_ID)
    except KeyError:
        raise HTTPException(status_code=404, detail="No browser")
    except Exception as e:
        logger.error(f"Resume failed: {e}")
        raise HTTPException(status_code=503, detail="Chrome did not start")
    return {"status": "ok"}


@app.get("/version")
async def version(request: Request) -> dict:
    _require_local(request)
    info = local_browser_manager.browsers.get(DEFAULT_BROWSER_ID)
    chrome = ""
    if info is not None and info.browser is not None:
        try:
            chrome = info.browser.version or ""
        except Exception:
            chrome = ""
    major = None
    if chrome.split(".")[0].isdigit():
        major = int(chrome.split(".")[0])
    paused = local_browser_manager.paused(DEFAULT_BROWSER_ID)
    return {
        "chrome": chrome,
        "chromeMajor": major,
        "image": IMAGE_VERSION,
        "pid": None if (info is None or paused) else _chrome_pid(info.chrome_port),
        "startedAt": None if info is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.started_at)),
        "paused": paused,
    }


# ── Browser CRUD ──

@app.get("/browsers")
async def list_browsers() -> list[BrowserResponse]:
    return [
        BrowserResponse(
            browser_id=bid,
            cdp_port=info.proxy_port,
            ws_endpoint=info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{bid}",
            profile_path=str(info.profile_path) if info.profile_path else None,
        )
        for bid, info in local_browser_manager.browsers.items()
    ]

@app.post("/browsers")
async def create_browser(request: CreateBrowserRequest = CreateBrowserRequest()) -> BrowserResponse:
    try:
        browser_id, info = await local_browser_manager.create_browser(
            profile_uid=request.profile_uid,
            proxy=request.proxy,
            extensions=request.extensions,
            cookies=request.cookies
        )
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=info.proxy_port,
            ws_endpoint=info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{browser_id}",
            profile_path=str(info.profile_path) if info.profile_path else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/browsers/{browser_id:path}")
async def delete_browser(browser_id: str) -> dict:
    try:
        success = await local_browser_manager.close_browser(browser_id)
        if success:
            return {"status": "success", "message": f"Browser '{browser_id}' closed"}
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/browsers/{browser_id:path}/restart")
async def restart_browser(browser_id: str) -> BrowserResponse:
    """Restart a browser, preserving its profile (extensions, cookies, etc.)."""
    try:
        info = await local_browser_manager.restart_browser(browser_id)
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=info.proxy_port,
            ws_endpoint=info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{browser_id}",
            profile_path=str(info.profile_path) if info.profile_path else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ── Cookies ──

@app.get("/browsers/{browser_id:path}/cookies")
async def get_cookies(browser_id: str) -> list[dict]:
    try:
        info = local_browser_manager.get_browser(browser_id)
        cookies = await info.context.cookies()
        return cookies
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/browsers/{browser_id:path}/cookies")
async def set_cookies(browser_id: str, cookies: list[dict]) -> dict:
    try:
        info = local_browser_manager.get_browser(browser_id)
        await info.context.add_cookies(cookies)
        return {"status": "success", "message": f"Added {len(cookies)} cookies"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Extensions ──

@app.get("/browsers/{browser_id:path}/extensions")
async def get_extensions(browser_id: str) -> list[dict]:
    """List extensions installed in a browser's profile."""
    try:
        info = local_browser_manager.get_browser(browser_id)
        if not info.profile_path:
            return []
        return list_profile_extensions(info.profile_path)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")

@app.post("/browsers/{browser_id:path}/extensions")
async def add_extensions(browser_id: str, request: ExtensionsRequest) -> BrowserResponse:
    """Inject extensions into a browser's profile and restart it automatically."""
    try:
        info = local_browser_manager.get_browser(browser_id)
        if not info.profile_path:
            raise HTTPException(status_code=400, detail="Cannot add extensions to an ephemeral browser without a profile. Create it with a profile_uid or with extensions.")

        # Download first, then pass to restart which injects AFTER Chrome exits
        ext_pairs = []
        for ext_id in request.extensions:
            cache_path = await download_extension(ext_id)
            ext_pairs.append((ext_id, cache_path))

        new_info = await local_browser_manager.restart_browser(browser_id, inject_extensions=ext_pairs)
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=new_info.proxy_port,
            ws_endpoint=new_info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{browser_id}",
            profile_path=str(new_info.profile_path) if new_info.profile_path else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/browsers/{browser_id:path}/extensions/{extension_id}")
async def delete_extension(browser_id: str, extension_id: str) -> BrowserResponse:
    """Remove an extension from a browser's profile and restart it automatically."""
    try:
        info = local_browser_manager.get_browser(browser_id)
        if not info.profile_path:
            raise HTTPException(status_code=400, detail="Cannot remove extensions from an ephemeral browser without a profile.")

        new_info = await local_browser_manager.restart_browser(browser_id, remove_extensions=[extension_id])
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=new_info.proxy_port,
            ws_endpoint=new_info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{browser_id}",
            profile_path=str(new_info.profile_path) if new_info.profile_path else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

class ToggleExtensionRequest(BaseModel):
    enabled: bool

@app.patch("/browsers/{browser_id:path}/extensions/{extension_id}")
async def toggle_extension(browser_id: str, extension_id: str, request: ToggleExtensionRequest) -> BrowserResponse:
    """Enable or disable an extension without removing it. Restarts the browser automatically."""
    try:
        info = local_browser_manager.get_browser(browser_id)
        if not info.profile_path:
            raise HTTPException(status_code=400, detail="Cannot toggle extensions on an ephemeral browser without a profile.")

        new_info = await local_browser_manager.restart_browser(
            browser_id,
            toggle_extensions=[(extension_id, request.enabled)]
        )
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=new_info.proxy_port,
            ws_endpoint=new_info.ws_endpoint,
            ws_stable_endpoint=f"{STABLE_WS_PREFIX}/{browser_id}",
            profile_path=str(new_info.profile_path) if new_info.profile_path else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Browser or extension '{extension_id}' not found")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")
