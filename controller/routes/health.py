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
    for bid, info in browser_manager.browsers.items():
        try:
            if not info.browser.is_connected():
                return Response(status_code=503, content=f"Browser {bid} disconnected")
        except Exception as e:
            return Response(status_code=503, content=f"Browser {bid} error: {e}")
    return {"status": "ok"}
