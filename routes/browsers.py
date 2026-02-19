import uuid
import logging
from typing import List

from fastapi import APIRouter, HTTPException

from core.browser import browser_manager
from core.dependencies import BrowserInfoDep, SessionIdDep, BrowserIdDep
from models.requests import CreateBrowserRequest, StartSessionRequest
from models.responses import BrowserResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browsers & Sessions"])


@router.get("/browsers")
async def list_browsers() -> List[BrowserResponse]:
    """List all active browsers."""
    return [
        BrowserResponse(
            browser_id=bid,
            profile_path=str(info.profile_path) if info.profile_path else None,
            session_count=len(info.pages),
        )
        for bid, info in browser_manager.browsers.items()
    ]


@router.post("/browsers")
async def create_browser(request: CreateBrowserRequest = CreateBrowserRequest()) -> BrowserResponse:
    """Create a new browser instance (persistent with profile_uid, or ephemeral)."""
    try:
        browser_id, browser_info = await browser_manager.create_browser(
            profile_uid=request.profile_uid,
            proxy=request.proxy,
        )
        return BrowserResponse(
            browser_id=browser_id,
            profile_path=str(browser_info.profile_path) if browser_info.profile_path else None,
            session_count=0,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/browsers/{browser_id:path}")
async def delete_browser(browser_id: str) -> dict:
    """Close and remove a browser instance. Cannot delete the default browser."""
    try:
        success = await browser_manager.close_browser(browser_id)
        if success:
            return {"status": "success", "message": f"Browser '{browser_id}' closed"}
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/start_session")
async def start_session(
    request: StartSessionRequest = StartSessionRequest(),
    browser_id: BrowserIdDep = None,
) -> dict:
    """Start a new session (page) in a browser and return the session ID."""
    bid = browser_id or request.browser_id or browser_manager.get_default_browser_id()

    try:
        browser_info = browser_manager.get_browser(bid)
    except KeyError:
        logger.info(f"Browser '{bid}' not found, creating it automatically")
        try:
            _, browser_info = await browser_manager.create_browser(profile_uid=bid)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create browser '{bid}': {str(e)}")

    session_id = str(uuid.uuid4())
    page = await browser_info.context.new_page()
    browser_info.pages[session_id] = page
    logger.info(f"Started new session: {session_id} in browser '{bid}'")

    return {
        "session_id": session_id,
        "browser_id": bid,
        "message": "Session created. Use X-Session-Id and X-Browser-Id headers in subsequent requests.",
    }


@router.delete("/end_session")
async def end_session(
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None,
) -> dict:
    """End a session and close its page. Requires X-Session-Id header."""
    if session_id is None:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required")

    page = browser_info.pages.pop(session_id, None)
    if page:
        try:
            await page.close()
            logger.info(f"Closed page for session {session_id}")
            return {"status": "success", "message": f"Session {session_id} ended"}
        except Exception as e:
            logger.warning(f"Error closing page for session {session_id}: {e}")
            return {"status": "success", "message": f"Session {session_id} removed (page was already closed)"}
    return {"status": "success", "message": f"Session {session_id} not found (already ended or never existed)"}
