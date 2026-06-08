"""livellm-agent entrypoint.

Boots a small FastAPI surface so the pod is healthy as soon as it starts, even
before a task is assigned. Task execution (`/act`), the browser-use engine, the
control channel and recording land in the runner (P1 cont.) — see
docs/browser-agent-architecture.md.
"""

import logging

import uvicorn
from fastapi import FastAPI

from livellm_agent import __version__
from livellm_agent.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="livellm-agent", version=__version__)


@app.get("/health/ping", tags=["Health"])
async def ping() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready", tags=["Health"])
async def ready() -> dict:
    """Readiness reflects whether a CDP target is configured for this agent."""
    target = (
        "controller" if settings.uses_controller
        else "cdp" if settings.cdp_ws_url
        else None
    )
    return {
        "ready": target is not None,
        "target": target,
        "model": settings.model_provider,
        "recording": settings.recording_enabled,
    }


if __name__ == "__main__":
    logger.info("livellm-agent %s starting on %s:%d (target=%s)",
                __version__, settings.host, settings.port,
                "controller" if settings.uses_controller else settings.cdp_ws_url or "unset")
    uvicorn.run(app, host=settings.host, port=settings.port)
