import asyncio
import functools
import logging
from typing import Annotated, List, Optional, AsyncGenerator, Tuple

from fastapi import Depends, Header, HTTPException, Request
from patchright.async_api import Page

from core import browser as browser_mod
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
        # Sessions and health marks of a browser that left while it was not
        # connected.
        for sid, bid in list(manager.sessions.items()):
            if bid not in registry_ids:
                manager.sessions.pop(sid, None)
        for bid in [b for b in manager._unhealthy_until if b not in registry_ids]:
            manager._unhealthy_until.pop(bid, None)
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


async def resolve_page(
    request: Request, named: Optional[str] = None, session_id: Optional[str] = None
) -> Tuple[BrowserInfo, Page]:
    """Choose the browser for a call, make sure it is connected, and return
    it with the tab the call runs in.

    Three ways, in this order:

    1. ``X-Session-Id``: the session's own browser and its tab (a new tab
       there when it has none). A browser also named (by ``X-Browser-Id`` or
       the /browsers/<name>/ path) must be that one, or 409. An unknown
       session is 404.
    2. ``X-Browser-Id: <name>`` or the /browsers/<name>/ path prefix: that
       browser, in a new tab. 404 when it is not in the pool, 502 when it
       cannot be reached.
    3. Nothing named: the browser with the fewest open tabs, counting calls in
       flight, over every browser in the pool, connected or not. One that
       cannot be reached, is slow to connect, or does not open a tab within
       a few seconds is skipped for a while and the next one is tried within
       the same call; 503 only when the pool is empty or none can be reached.

    For 2 and 3 the tab is new and the caller owns it. For 1 it is the
    session's tab; a new one is not yet recorded on the session.

    Source of truth for ws_url is the file-backed registry (an operator-maintained
    file mapping browser_id -> stable Service ws_url). On every request we
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
            info = await _connect(manager, named)
        except Exception as e:
            # The error text can carry the browser's internal address.
            logger.warning(f"Browser '{named}' could not be reached: {type(e).__name__}: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Browser '{named}' is not reachable right now.",
            )
        if session_id is not None:
            page = info.pages.get(session_id)
            if page is not None:
                if not page.is_closed():
                    return info, page
                logger.info(f"Session page {session_id} was closed, creating new one")
        return await open_page(manager, info)

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
            info = await _connect(manager, bid, picked=True)
            return info, await manager.open_tab(info)
        except Exception as e:
            logger.warning(
                f"Browser '{bid}' could not be reached, trying another: {type(e).__name__}: {e}"
            )
            _release(request)
            tried.append(bid)


async def _connect(manager: BrowserManager, bid: str, picked: bool = False) -> BrowserInfo:
    return await manager.ensure_connected(
        bid,
        browser_registry.get_browser_ws_url(bid),
        headers=browser_registry.get_browser_headers(bid),
        picked=picked,
    )


async def open_page(manager: BrowserManager, browser_info: BrowserInfo) -> Tuple[BrowserInfo, Page]:
    """Open a tab on a named browser, reconnecting once if that fails.

    Both the tab and the reconnect are bounded (TAB_TIMEOUT, CONNECT_TIMEOUT),
    and a failed tab drops the connection first, so the reconnect is a real
    one. Returns the (possibly rebuilt) BrowserInfo with the new page.
    """
    try:
        return browser_info, await manager.open_tab(browser_info)
    except Exception:
        pass
    bid = browser_info.browser_id
    reconnect_url = browser_registry.get_browser_ws_url(bid) or browser_info.ws_url
    try:
        browser_info = await manager.recover_connection(
            bid, reconnect_url, headers=browser_registry.get_browser_headers(bid),
        )
        return browser_info, await manager.open_tab(browser_info)
    except Exception as recover_err:
        logger.error(
            f"Failed to open a tab on '{bid}' after recovery: "
            f"{type(recover_err).__name__}: {recover_err}"
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not open a tab on browser '{bid}'.",
        )


async def close_page(page: Page, what: str) -> None:
    """Close a tab without ever holding the call up on a browser that is gone."""
    try:
        await asyncio.wait_for(page.close(), timeout=browser_mod.DISCONNECT_TIMEOUT)
    except Exception as e:
        logger.warning(f"Error closing {what}: {type(e).__name__}: {e}")


async def get_or_create_page(
    request: Request,
    browser_id: BrowserIdDep = None,
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
    browser_info, page = await resolve_page(request, browser_id, session_id)
    is_ad_hoc = session_id is None

    if not is_ad_hoc and browser_info.pages.get(session_id) is not page:
        if manager.session_browser(session_id) != browser_info.browser_id:
            # The session ended, or its browser left, while the tab opened:
            # keeping the tab would leave it open with no session to close it.
            await close_page(page, f"the tab of ended session {session_id}")
            raise unknown_session(session_id)
        manager.add_session(browser_info.browser_id, session_id, page)
        logger.info(f"Opened a tab for session {session_id} on '{browser_info.browser_id}'")

    try:
        yield page
    finally:
        if is_ad_hoc:
            await close_page(page, "an ad-hoc page")


PageDep = Annotated[Page, Depends(get_or_create_page)]
