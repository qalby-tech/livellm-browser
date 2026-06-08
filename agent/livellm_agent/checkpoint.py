"""Per-step state checkpoint — snapshot/restore browser state via raw CDP so
restart-from-step is exact (not a fragile history replay).

Coded against browser-use==0.12.9 + cdp-use==1.4.5. The page-scoped CDP calls
go through `session.get_or_create_cdp_session()` which yields the active page's
`session_id`; everything is best-effort (CDP errors raise RuntimeError — we log
and continue rather than fail the run).

A checkpoint captures {url, cookies, local_storage}. Cookies are browser-wide
(`Storage.getCookies`); localStorage is origin-scoped, so on restore we set
cookies → navigate → write localStorage → reload so the app boots with it.

NOTE: checkpoints contain session cookies/tokens. They are persisted in the
step row (checkpoint_json) like any restart state; treat that store as
sensitive (it lives in the per-tenant browser_agent DB).
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _ctx(session, focus: bool = False):
    cdp = await session.get_or_create_cdp_session(target_id=None, focus=focus)
    return cdp.session_id, cdp.cdp_client


async def _read_cookies(session) -> list[dict]:
    sid, client = await _ctx(session)
    resp = await client.send.Storage.getCookies(session_id=sid)
    return resp.get("cookies", [])


async def _read_local_storage(session) -> dict[str, str]:
    sid, client = await _ctx(session)
    resp = await client.send.Runtime.evaluate(
        params={
            "expression": "JSON.stringify(window.localStorage)",
            "returnByValue": True,
            "awaitPromise": True,
        },
        session_id=sid,
    )
    if "exceptionDetails" in resp:
        raise RuntimeError(resp["exceptionDetails"])
    raw = resp["result"].get("value")
    return json.loads(raw) if raw else {}


async def _current_url(session) -> str:
    info = await session.get_current_target_info()
    if info and info.get("url"):
        return info["url"]
    sid, client = await _ctx(session)
    h = await client.send.Page.getNavigationHistory(session_id=sid)
    return h["entries"][h["currentIndex"]]["url"]


async def snapshot(session) -> dict[str, Any]:
    """Capture {url, cookies, local_storage}; never raises (best-effort)."""
    cp: dict[str, Any] = {}
    try:
        cp["url"] = await _current_url(session)
    except Exception as e:
        logger.debug("checkpoint: url read failed: %s", e)
    try:
        cp["cookies"] = await _read_cookies(session)
    except Exception as e:
        logger.debug("checkpoint: cookie read failed: %s", e)
    try:
        cp["local_storage"] = await _read_local_storage(session)
    except Exception as e:
        logger.debug("checkpoint: localStorage read failed: %s", e)
    return cp


async def restore(session, cp: dict[str, Any]) -> None:
    """Restore a snapshot: cookies → navigate → localStorage → reload.
    Best-effort; logs and continues on any CDP error."""
    if not cp:
        return
    sid, client = await _ctx(session, focus=True)

    cookies = cp.get("cookies")
    if cookies:
        try:
            await client.send.Network.setCookies(params={"cookies": cookies}, session_id=sid)
        except Exception as e:
            logger.warning("checkpoint restore: setCookies failed: %s", e)

    url = cp.get("url")
    if url:
        try:
            await client.send.Page.navigate(params={"url": url}, session_id=sid)
        except Exception as e:
            logger.warning("checkpoint restore: navigate failed: %s", e)

    ls = cp.get("local_storage")
    if ls:
        try:
            expr = (
                "(() => { const d = JSON.parse(" + json.dumps(json.dumps(ls)) + ");"
                " window.localStorage.clear();"
                " for (const k in d) window.localStorage.setItem(k, d[k]);"
                " return true; })()"
            )
            r = await client.send.Runtime.evaluate(
                params={"expression": expr, "returnByValue": True}, session_id=sid
            )
            if "exceptionDetails" in r:
                logger.warning("checkpoint restore: localStorage write: %s", r["exceptionDetails"])
            elif url:
                # reload so the app reads the restored localStorage
                await client.send.Page.navigate(params={"url": url}, session_id=sid)
        except Exception as e:
            logger.warning("checkpoint restore: localStorage failed: %s", e)
