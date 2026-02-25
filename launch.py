import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
from patchright.async_api import async_playwright

from core.local_browser import local_browser_manager, PROFILES_DIR, DEFAULT_BROWSER_ID, cleanup_profile_locks

class ProxySettings(BaseModel):
    server: str
    username: Optional[str] = None
    password: Optional[str] = None
    bypass: Optional[str] = None

class CreateBrowserRequest(BaseModel):
    profile_uid: Optional[str] = None
    proxy: Optional[ProxySettings] = None

class BrowserResponse(BaseModel):
    browser_id: str
    cdp_port: int
    ws_endpoint: str
    profile_path: Optional[str] = None

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Prepare default profile directory
    default_profile = PROFILES_DIR / DEFAULT_BROWSER_ID
    cleanup_profile_locks(default_profile)
    default_profile.mkdir(parents=True, exist_ok=True)
    
    # Start Playwright + local browser manager
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

@app.get("/browsers")
async def list_browsers() -> list[BrowserResponse]:
    return [
        BrowserResponse(
            browser_id=bid,
            cdp_port=info.proxy_port,
            ws_endpoint=info.ws_endpoint,
            profile_path=str(info.profile_path) if info.profile_path else None
        )
        for bid, info in local_browser_manager.browsers.items()
    ]

@app.post("/browsers")
async def create_browser(request: CreateBrowserRequest = CreateBrowserRequest()) -> BrowserResponse:
    try:
        browser_id, info = await local_browser_manager.create_browser(
            profile_uid=request.profile_uid,
            proxy=request.proxy
        )
        return BrowserResponse(
            browser_id=browser_id,
            cdp_port=info.proxy_port,
            ws_endpoint=info.ws_endpoint,
            profile_path=str(info.profile_path) if info.profile_path else None
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")