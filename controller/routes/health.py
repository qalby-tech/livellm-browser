from fastapi import APIRouter
from fastapi.responses import Response
from models.responses import PingResponse
from core.browser import browser_manager

router = APIRouter(tags=["Health"])


@router.get("/ping")
async def ping() -> PingResponse:
    return PingResponse()


@router.get("/healthz")
async def healthz():
    # A dead Node driver leaves every connection of its engine unusable and,
    # once a failed recovery dropped them, the loop below is vacuous — check
    # the drivers themselves (Chrome's, and Camoufox's once started) so the
    # liveness probe restarts the pod.
    dead = browser_manager.dead_drivers()
    if dead:
        return Response(status_code=503, content=f"Playwright driver is not running ({', '.join(dead)})")
    # One browser that dropped is that browser's problem: calls reconnect it
    # or go elsewhere, and restarting this pod would end every session on
    # every browser. Only when all of them dropped is the pod suspect.
    ids = list(browser_manager.browsers)
    if ids and not any(browser_manager.is_connected(bid) for bid in ids):
        return Response(status_code=503, content="No browser connection is alive")
    return {"status": "ok"}
