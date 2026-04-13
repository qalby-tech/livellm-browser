import uuid
import logging
from typing import Annotated, Optional, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from patchright.async_api import Page

from core.browser import BrowserInfo, BrowserManager
from core.redis_state import redis_controller_state

logger = logging.getLogger(__name__)

# Header dependencies
BrowserIdDep = Annotated[Optional[str], Header(alias="X-Browser-Id")]
SessionIdDep = Annotated[Optional[str], Header(alias="X-Session-Id")]


async def get_browser_info(
    request: Request,
    browser_id: BrowserIdDep = None,
) -> BrowserInfo:
    """
    Resolve browser info from X-Browser-Id header.
    Falls back to the first connected browser.  Returns 404 if none available.
    Auto-reconnects if the browser connection died (e.g. after a restart).
    """
    manager: BrowserManager = request.app.state.browser_manager

    bid = browser_id
    if not bid:
        bid = manager.first_browser_id()
        if not bid:
            raise HTTPException(
                status_code=404,
                detail="No browsers connected. Register one first via POST /browsers.",
            )

    try:
        info = manager.get_browser(bid)
    except KeyError:
        # Try to auto-discover from Redis
        ws_url = await redis_controller_state.get_browser_ws_url(bid)
        if ws_url:
            logger.info(f"Auto-discovered browser '{bid}' from Redis, connecting...")
            try:
                info = await manager.connect_browser(bid, ws_url)
            except Exception as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Browser '{bid}' found in Redis but connection failed: {e}",
                )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Browser '{bid}' not found. Ensure the browser is running.",
            )

    if not info.browser.is_connected():
        logger.info(f"Browser '{bid}' connection is dead, auto-reconnecting...")
        # Try Redis first for a fresh WS URL
        fresh_ws_url = await redis_controller_state.get_browser_ws_url(bid)
        reconnect_url = fresh_ws_url or info.ws_url
        try:
            info = await manager.recover_connection(bid, reconnect_url)
        except Exception as e:
            logger.error(f"Failed to reconnect browser '{bid}': {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Browser '{bid}' disconnected and reconnection failed: {e}",
            )

    return info


BrowserInfoDep = Annotated[BrowserInfo, Depends(get_browser_info)]


async def get_or_create_page(
    request: Request,
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None,
) -> AsyncGenerator[Page, None]:
    """
    Get or create a page for the given session.
    If no session_id header is provided, creates an ad-hoc session (closed after request).
    """
    is_ad_hoc = False
    if session_id is None:
        session_id = str(uuid.uuid4())
        is_ad_hoc = True

    request.state.session_id = session_id
    pages = browser_info.pages
    page = None

    if session_id in pages:
        page = pages[session_id]
        try:
            _ = page.url
        except Exception:
            logger.info(f"Page for session {session_id} was closed, creating new one")
            page = None

    if page is None:
        try:
            page = await browser_info.context.new_page()
        except Exception as e:
            logger.warning(
                f"Failed to create page for session {session_id}: {e} — attempting recovery"
            )
            manager: BrowserManager = request.app.state.browser_manager
            try:
                browser_info = await manager.recover_connection(
                    browser_info.browser_id, browser_info.ws_url
                )
                pages = browser_info.pages
            except Exception as recover_err:
                logger.error(f"Recovery failed: {recover_err}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Browser connection lost and recovery failed: {recover_err}",
                )
            page = await browser_info.context.new_page()
        pages[session_id] = page
        logger.info(f"Created new page for session {session_id} (ad-hoc={is_ad_hoc})")

    try:
        yield page
    finally:
        if is_ad_hoc:
            try:
                pages.pop(session_id, None)
                await page.close()
                logger.info(f"Closed ad-hoc page for session {session_id}")
            except Exception as e:
                logger.warning(f"Error closing ad-hoc page for session {session_id}: {e}")


PageDep = Annotated[Page, Depends(get_or_create_page)]
