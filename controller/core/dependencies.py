import functools
import logging
from typing import Annotated, List, Optional, AsyncGenerator, Tuple

from fastapi import Depends, Header, HTTPException, Request
from patchright.async_api import Page

from core.browser import BrowserInfo, BrowserManager
from core.registry import browser_registry, managed

logger = logging.getLogger(__name__)

# Header dependencies
BrowserIdDep = Annotated[Optional[str], Header(alias="X-Browser-Id")]
SessionIdDep = Annotated[Optional[str], Header(alias="X-Session-Id")]


# ==================== Pool ====================

async def browser_pool(manager: BrowserManager) -> List[str]:
    """The browsers a call may land on, in registry order.

    Managed (BROWSERS_CONFIG set): exactly the registry. A connected browser
    that has left it is disconnected here, with its sessions, so taking a
    browser out is immediate. Standalone: the registry plus whatever was
    connected through POST /browsers.

    The registry is re-read on every call (cached by mtime), so a browser
    added to it is picked from the next call on.
    """
    registry_ids = list(browser_registry.get_all_browsers())
    if managed():
        for bid in [b for b in manager.browsers if b not in registry_ids]:
            logger.info(f"Browser '{bid}' left the registry, disconnecting")
            await manager.remove_browser(bid)
        # Sessions of a browser that left while it was not connected.
        for sid, bid in list(manager.sessions.items()):
            if bid not in registry_ids:
                manager.sessions.pop(sid, None)
        return registry_ids
    return registry_ids + [b for b in manager.browsers if b not in registry_ids]


# ==================== Resolution ====================

def _hold(request: Request, manager: BrowserManager, bid: str) -> None:
    """Name ``bid`` as the answering browser and keep its call counted until
    the response is sent (core.middleware releases it). The call must already
    be counted (pick_browser or begin_call)."""
    _release(request)
    request.state.browser_id = bid
    request.state.release_browser = functools.partial(manager.end_call, bid)


def _release(request: Request) -> None:
    release = getattr(request.state, "release_browser", None)
    if release is not None:
        release()
        request.state.release_browser = None
    request.state.browser_id = None


def unknown_session(session_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=f"Session '{session_id}' not found. Start one with POST /start_session.",
    )


def session_owner(
    request: Request, manager: BrowserManager, session_id: str, named: Optional[str]
) -> str:
    """The browser a session lives on. 404 for an unknown session, 409 when
    the call names another browser."""
    owner = manager.session_browser(session_id)
    if owner is None:
        raise unknown_session(session_id)
    if named and named != owner:
        request.state.browser_id = owner
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session '{session_id}' is on browser '{owner}', not '{named}'. "
                f"Send X-Session-Id alone, or name '{owner}'."
            ),
        )
    return owner


async def resolve_browser(
    request: Request, named: Optional[str] = None, session_id: Optional[str] = None
) -> BrowserInfo:
    """Choose the browser for a call and make sure it is connected.

    Three ways, in this order:

    1. ``X-Session-Id``: the session's own browser. A browser also named (by
       ``X-Browser-Id`` or the /browsers/<name>/ path) must be that one, or
       409. An unknown session is 404.
    2. ``X-Browser-Id: <name>`` or the /browsers/<name>/ path prefix: that
       browser. 404 when it is not in the pool, 502 when it cannot be reached.
    3. Nothing named: the browser with the fewest open tabs, counting calls in
       flight, over every browser in the pool, connected or not. One that
       cannot be reached, or takes longer than a few seconds to connect, is
       skipped for a while and the next one is tried; 503 only when the pool
       is empty or none can be reached.

    Source of truth for ws_url is the file-backed registry (an operator-maintained
    ConfigMap mapping browser_id -> stable Service ws_url). On every request we
    cheaply re-read it and reconnect if the local connection is dead, so that
    local state can never silently lag behind cluster state.
    """
    manager: BrowserManager = request.app.state.browser_manager
    pool = await browser_pool(manager)

    if session_id is not None:
        named = session_owner(request, manager, session_id, named)

    if named:
        if named not in pool:
            raise HTTPException(
                status_code=404,
                detail=f"Browser '{named}' not found. Ensure the browser is running.",
            )
        manager.begin_call(named)
        _hold(request, manager, named)
        try:
            return await _connect(manager, named)
        except Exception as e:
            # The error text can carry the browser's internal address.
            logger.warning(f"Browser '{named}' could not be reached: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Browser '{named}' is not reachable right now.",
            )

    if not pool:
        detail = (
            "This Browser API has no browsers yet."
            if managed()
            else "No browsers available. Register one first via POST /browsers."
        )
        raise HTTPException(status_code=503, detail=detail)

    tried: List[str] = []
    while True:
        bid = manager.pick_browser(pool, exclude=tried)
        if bid is None:
            raise HTTPException(
                status_code=503,
                detail="None of this Browser API's browsers can be reached right now.",
            )
        _hold(request, manager, bid)
        try:
            return await _connect(manager, bid, picked=True)
        except Exception as e:
            logger.warning(f"Browser '{bid}' could not be reached, trying another: {e!r}")
            _release(request)
            tried.append(bid)


async def _connect(manager: BrowserManager, bid: str, picked: bool = False) -> BrowserInfo:
    return await manager.ensure_connected(
        bid,
        browser_registry.get_browser_ws_url(bid),
        headers=browser_registry.get_browser_headers(bid),
        picked=picked,
    )


async def get_browser_info(
    request: Request,
    browser_id: BrowserIdDep = None,
    session_id: SessionIdDep = None,
) -> BrowserInfo:
    """Resolve the browser for this call; see ``resolve_browser``."""
    return await resolve_browser(request, browser_id, session_id)


BrowserInfoDep = Annotated[BrowserInfo, Depends(get_browser_info)]


async def open_page(manager: BrowserManager, browser_info: BrowserInfo) -> Tuple[BrowserInfo, Page]:
    """Open a tab, reconnecting once if the connection turns out to be dead.

    Returns the (possibly rebuilt) BrowserInfo with the new page.
    """
    try:
        return browser_info, await browser_info.context.new_page()
    except Exception as e:
        logger.warning(f"Failed to create page, attempting recovery: {e}")
    bid = browser_info.browser_id
    reconnect_url = browser_registry.get_browser_ws_url(bid) or browser_info.ws_url
    try:
        browser_info = await manager.recover_connection(
            bid, reconnect_url, headers=browser_registry.get_browser_headers(bid),
        )
        return browser_info, await browser_info.context.new_page()
    except Exception as recover_err:
        logger.error(f"Failed to open a tab on '{bid}' after recovery: {recover_err}")
        raise HTTPException(
            status_code=502,
            detail=f"Could not open a tab on browser '{bid}'.",
        )


async def get_or_create_page(
    request: Request,
    browser_info: BrowserInfoDep,
    session_id: SessionIdDep = None,
) -> AsyncGenerator[Page, None]:
    """
    Get a page for the request.

    • **Session** (``X-Session-Id`` provided) — the session's tab on its own
      browser; a new tab there if the old one was closed or the browser
      reconnected.
    • **Ad-hoc** (no session header) — creates a fresh page just for this
      request and closes it on the way out.
    """
    manager: BrowserManager = request.app.state.browser_manager
    is_ad_hoc = session_id is None
    page: Optional[Page] = None

    # ── Session: reuse its tab ──
    if not is_ad_hoc and session_id in browser_info.pages:
        page = browser_info.pages[session_id]
        if page.is_closed():
            logger.info(f"Session page {session_id} was closed, creating new one")
            page = None

    if page is None:
        browser_info, page = await open_page(manager, browser_info)
        if not is_ad_hoc:
            if manager.session_browser(session_id) != browser_info.browser_id:
                # The session ended, or its browser left, while the tab opened:
                # keeping the tab would leave it open with no session to close it.
                try:
                    await page.close()
                except Exception:
                    pass
                raise unknown_session(session_id)
            manager.add_session(browser_info.browser_id, session_id, page)
            logger.info(f"Opened a tab for session {session_id} on '{browser_info.browser_id}'")

    try:
        yield page
    finally:
        if is_ad_hoc:
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"Error closing ad-hoc page: {e}")


PageDep = Annotated[Page, Depends(get_or_create_page)]
