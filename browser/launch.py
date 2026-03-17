import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from patchright.async_api import async_playwright

from core.const import PROFILES_DIR, DEFAULT_BROWSER_ID, STABLE_WS_PREFIX
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
    profile_uid: Optional[str] = None
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    default_profile = PROFILES_DIR / DEFAULT_BROWSER_ID
    cleanup_profile_locks(default_profile)
    default_profile.mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()
    try:
        await local_browser_manager.start(playwright)
    except Exception as e:
        logger.error(f"Failed to start local browser manager: {e}")

    app.state.playwright = playwright

    yield

    logger.info("Application shutting down, cleaning up resources...")
    try:
        await local_browser_manager.shutdown(timeout=25.0)
    except Exception as e:
        logger.error(f"Error during browser shutdown: {e}")
    try:
        await asyncio.wait_for(playwright.stop(), timeout=5.0)
    except Exception as e:
        logger.warning(f"Error stopping playwright: {e}")
    logger.info("Shutdown complete")

app = FastAPI(title="Browser Launcher API", lifespan=lifespan)

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
