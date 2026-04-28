import uuid
import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request

from core.browser import browser_manager
from core.dependencies import (
    BrowserInfoDep, SessionIdDep, BrowserIdDep, get_browser_info,
)
from core.redis_state import redis_controller_state
from models.requests import ConnectBrowserRequest, StartSessionRequest
from models.responses import BrowserResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browsers & Sessions"])


@router.get("/browsers")
async def list_browsers() -> List[BrowserResponse]:
    """List all connected browsers."""
    return [
        BrowserResponse(
            browser_id=bid,
            ws_url=info.ws_url,
            session_count=len(info.pages),
        )
        for bid, info in browser_manager.browsers.items()
    ]


@router.post("/browsers")
async def connect_browser(request: ConnectBrowserRequest) -> BrowserResponse:
    """
    Connect to a remote browser via its CDP WebSocket URL.

    Idempotent: if the same ``browser_id`` + ``ws_url`` pair is already
    connected the existing connection is returned.  If the ``browser_id``
    exists but with a **different** URL, the old connection is dropped and
    replaced.
    """
    try:
        info = await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except ValueError:
        # Already connected with a different URL → reconnect
        await browser_manager.disconnect_browser(request.browser_id)
        info = await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect: {e}")

    return BrowserResponse(
        browser_id=request.browser_id,
        ws_url=info.ws_url,
        session_count=len(info.pages),
    )


@router.delete("/browsers/{browser_id:path}")
async def disconnect_browser(browser_id: str) -> dict:
    """Disconnect a browser and close all its sessions."""
    success = await browser_manager.disconnect_browser(browser_id)
    if success:
        return {"status": "success", "message": f"Browser '{browser_id}' disconnected"}
    raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not connected")


@router.post("/start_session")
async def start_session(
    request: Request,
    body: StartSessionRequest = StartSessionRequest(),
    browser_id: BrowserIdDep = None,
) -> dict:
    """Start a new session (page) in a connected browser and return the session ID.

    Routes through the same Redis-backed resolution as the rest of the API,
    so a stale local connection (e.g. browser pod restarted with a new IP)
    is auto-recovered before we try to open a page.
    """
    bid = browser_id or body.browser_id
    try:
        browser_info = await get_browser_info(request=request, browser_id=bid)
    except HTTPException:
        raise

    try:
        page = await browser_info.context.new_page()
    except Exception as e:
        logger.warning(f"start_session: failed to open page, attempting recovery: {e}")
        fresh_ws_url = await redis_controller_state.get_browser_ws_url(browser_info.browser_id)
        reconnect_url = fresh_ws_url or browser_info.ws_url
        try:
            browser_info = await browser_manager.recover_connection(
                browser_info.browser_id, reconnect_url
            )
            page = await browser_info.context.new_page()
        except Exception as recover_err:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to start session after recovery: {recover_err}",
            )

    session_id = str(uuid.uuid4())
    browser_info.pages[session_id] = page
    logger.info(f"Started new session: {session_id} in browser '{browser_info.browser_id}'")

    return {
        "session_id": session_id,
        "browser_id": browser_info.browser_id,
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
