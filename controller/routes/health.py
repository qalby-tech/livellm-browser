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
    # A dead Node driver leaves every CDP connection unusable and, once the
    # browsers dict is emptied by a failed recovery, the loop below is vacuous
    # — check the driver itself so the liveness probe restarts the pod.
    if not browser_manager.driver_alive():
        return Response(status_code=503, content="Playwright driver is not running")
    # One browser that dropped is that browser's problem: calls reconnect it
    # or go elsewhere, and restarting this pod would end every session on
    # every browser. Only when all of them dropped is the pod suspect.
    ids = list(browser_manager.browsers)
    if ids and not any(browser_manager.is_connected(bid) for bid in ids):
        return Response(status_code=503, content="No browser connection is alive")
    return {"status": "ok"}
