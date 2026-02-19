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
    Defaults to the default browser. Auto-creates if not found.
    """
    manager: BrowserManager = request.app.state.browser_manager
    bid = browser_id or manager.get_default_browser_id()
    try:
        return manager.get_browser(bid)
    except KeyError:
        logger.info(f"Browser '{bid}' not found, creating it automatically")
        try:
            _, browser_info = await manager.create_browser(profile_uid=bid)
            return browser_info
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create browser '{bid}': {str(e)}")


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
