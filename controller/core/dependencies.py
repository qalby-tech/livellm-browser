import uuid
import logging
from typing import Annotated, Optional, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from patchright.async_api import Page

from core.browser import BrowserInfo, BrowserManager

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
        raise HTTPException(
            status_code=404,
            detail=f"Browser '{bid}' not connected. Register it first via POST /browsers.",
        )

    if not info.browser.is_connected():
        logger.info(f"Browser '{bid}' connection is dead, auto-reconnecting...")
        try:
            info = await manager.connect_browser(bid, info.ws_url)
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
