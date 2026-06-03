import logging
from typing import Annotated, Optional, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from patchright.async_api import Page

from core.browser import BrowserInfo, BrowserManager
from core.registry import browser_registry

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

    Source of truth for ws_url is the file-backed registry (an operator-maintained
    ConfigMap mapping browser_id -> stable Service ws_url). On every request we
    cheaply re-read it and reconnect if the local connection is dead, so that
    local state can never silently lag behind cluster state.
    """
    manager: BrowserManager = request.app.state.browser_manager

    bid = browser_id
    if not bid:
        bid = manager.least_loaded_browser_id() or manager.first_browser_id()
        if not bid:
            # Nothing connected yet — fall back to the first registered browser.
            bid = next(iter(browser_registry.get_all_browsers()), None)
        if not bid:
            raise HTTPException(
                status_code=404,
                detail="No browsers available. Register one first via POST /browsers.",
            )

    fresh_ws_url = browser_registry.get_browser_ws_url(bid)
    fresh_headers = browser_registry.get_browser_headers(bid)

    try:
        info = manager.get_browser(bid)
    except KeyError:
        if not fresh_ws_url:
            raise HTTPException(
                status_code=404,
                detail=f"Browser '{bid}' not found. Ensure the browser is running.",
            )
        logger.info(f"Auto-discovered browser '{bid}' from registry, connecting...")
        try:
            info = await manager.connect_browser(bid, fresh_ws_url, headers=fresh_headers)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Browser '{bid}' found in registry but connection failed: {e}",
            )
        return info

    # Check for ws_url drift (e.g. browser pod restarted with new IP/port).
    drifted = bool(fresh_ws_url) and fresh_ws_url != info.ws_url

    needs_recovery = drifted or not info.browser.is_connected()

    if needs_recovery:
        reconnect_url = fresh_ws_url or info.ws_url
        if drifted:
            logger.info(
                f"Browser '{bid}' ws_url drift "
                f"({info.ws_url} -> {fresh_ws_url}), reconnecting..."
            )
        else:
            logger.info(f"Browser '{bid}' connection is dead, auto-reconnecting...")
        try:
            info = await manager.recover_connection(bid, reconnect_url, headers=fresh_headers)
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
    Get a page for the request.

    • **Named session** (``X-Session-Id`` provided) — returns the existing
      session page, or creates a new one in the default context.
    • **Ad-hoc** (no session header) — creates a fresh page just for this
      request and closes it on the way out.
    """
    is_ad_hoc = session_id is None
    page: Optional[Page] = None

    # ── Named session: reuse existing page ──
    if not is_ad_hoc and session_id in browser_info.pages:
        page = browser_info.pages[session_id]
        try:
            _ = page.url
        except Exception:
            logger.info(f"Session page {session_id} was closed, creating new one")
            page = None

    if page is None:
        manager: BrowserManager = request.app.state.browser_manager
        try:
            page = await browser_info.context.new_page()
        except Exception as e:
            logger.warning(f"Failed to create page, attempting recovery: {e}")
            fresh_ws_url = browser_registry.get_browser_ws_url(
                browser_info.browser_id
            )
            reconnect_url = fresh_ws_url or browser_info.ws_url
            try:
                browser_info = await manager.recover_connection(
                    browser_info.browser_id, reconnect_url,
                    headers=browser_registry.get_browser_headers(browser_info.browser_id),
                )
                page = await browser_info.context.new_page()
            except Exception as recover_err:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to create page after recovery: {recover_err}",
                )

        if not is_ad_hoc:
            browser_info.pages[session_id] = page
            logger.info(f"Created new page for session {session_id}")

    try:
        yield page
    finally:
        if is_ad_hoc:
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing ad-hoc page: {e}")


PageDep = Annotated[Page, Depends(get_or_create_page)]
