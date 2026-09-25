import uuid
import logging
from typing import List

from fastapi import APIRouter, HTTPException, Request

from core.browser import browser_manager
from core.dependencies import (
    SessionIdDep, BrowserIdDep, browser_pool, close_page, resolve_page, session_owner,
)
from core.registry import managed
from models.requests import ConnectBrowserRequest, StartSessionRequest
from models.responses import BrowserResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Browsers & Sessions"])

# Management routes are /browsers and /browsers/{browser_id}, one segment at
# most: /browsers/<name>/<path> is the path form of X-Browser-Id, rewritten
# before routing (core/middleware.py).


def _status(browser_id: str) -> BrowserResponse:
    return BrowserResponse(
        browser_id=browser_id,
        connected=browser_manager.is_connected(browser_id),
        healthy=browser_manager.is_healthy(browser_id),
        open_tabs=browser_manager.open_tabs(browser_id),
        session_count=browser_manager.session_count(browser_id),
    )


def _refuse_when_managed() -> None:
    if managed():
        raise HTTPException(
            status_code=403,
            detail="Browsers join and leave this Browser API in its settings, not through this endpoint.",
        )


@router.get("/browsers")
async def list_browsers() -> List[BrowserResponse]:
    """Every browser calls can land on: connected or not, reachable or not, and its open tabs."""
    return [_status(bid) for bid in await browser_pool(browser_manager)]


@router.get("/browsers/{browser_id}")
async def get_browser(browser_id: str) -> BrowserResponse:
    """One browser's status."""
    if browser_id not in await browser_pool(browser_manager):
        raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not found")
    return _status(browser_id)


@router.post("/browsers")
async def connect_browser(request: ConnectBrowserRequest) -> BrowserResponse:
    """
    Connect to a remote browser via its CDP WebSocket URL.

    Only without a registry (BROWSERS_CONFIG unset); otherwise 403.
    Idempotent: if the same ``browser_id`` + ``ws_url`` pair is already
    connected the existing connection is returned.  If the ``browser_id``
    exists but with a **different** URL, the old connection is dropped and
    replaced.
    """
    _refuse_when_managed()
    try:
        await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except ValueError:
        # Already connected with a different URL → reconnect
        await browser_manager.disconnect_browser(request.browser_id)
        await browser_manager.connect_browser(
            browser_id=request.browser_id,
            ws_url=request.ws_url,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect: {e}")

    return _status(request.browser_id)


@router.delete("/browsers/{browser_id}")
async def disconnect_browser(browser_id: str) -> dict:
    """Disconnect a browser and end its sessions. Only without a registry; otherwise 403."""
    _refuse_when_managed()
    success = await browser_manager.remove_browser(browser_id)
    if success:
        return {"status": "success", "message": f"Browser '{browser_id}' disconnected"}
    raise HTTPException(status_code=404, detail=f"Browser '{browser_id}' not connected")


@router.post("/start_session")
async def start_session(
    request: Request,
    body: StartSessionRequest = StartSessionRequest(),
    browser_id: BrowserIdDep = None,
) -> dict:
    """Start a session (a tab) and return its ID.

    The browser is the one named (X-Browser-Id, the /browsers/<name>/ path or
    ``browser_id`` in the body), else the one with the fewest open tabs. Later
    calls with X-Session-Id alone go to that browser.
    """
    if browser_id and body.browser_id and body.browser_id != browser_id:
        raise HTTPException(
            status_code=400,
            detail=f"The body names browser '{body.browser_id}' but the call names '{browser_id}'.",
        )
    browser_info, page = await resolve_page(request, browser_id or body.browser_id)

    session_id = str(uuid.uuid4())
    browser_manager.add_session(browser_info.browser_id, session_id, page)
    logger.info(f"Started new session: {session_id} in browser '{browser_info.browser_id}'")

    return {
        "session_id": session_id,
        "browser_id": browser_info.browser_id,
        "message": "Session started. Send X-Session-Id on later calls; it stays on this browser.",
    }


@router.delete("/end_session")
async def end_session(
    request: Request,
    session_id: SessionIdDep = None,
    browser_id: BrowserIdDep = None,
) -> dict:
    """End a session and close its tab. Requires X-Session-Id; no browser needs naming."""
    if session_id is None:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required")

    await browser_pool(browser_manager)
    owner = session_owner(request, browser_manager, session_id, browser_id)
    request.state.browser_id = owner

    page = browser_manager.end_session(session_id)
    if page:
        await close_page(page, f"the tab of session {session_id}")
    return {"status": "success", "message": f"Session {session_id} ended", "browser_id": owner}
