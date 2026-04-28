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
    for bid, info in browser_manager.browsers.items():
        try:
            if not info.browser.is_connected():
                return Response(status_code=503, content=f"Browser {bid} disconnected")
        except Exception as e:
            return Response(status_code=503, content=f"Browser {bid} error: {e}")
    return {"status": "ok"}
