from fastapi import APIRouter
from models.responses import PingResponse

router = APIRouter(tags=["Health"])


@router.get("/ping")
async def ping() -> PingResponse:
    """Health check endpoint."""
    return PingResponse()
