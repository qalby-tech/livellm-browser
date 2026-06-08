"""livellm-agent entrypoint.

Boots a small FastAPI surface so the pod is healthy as soon as it starts, even
before a task is assigned. `POST /act` launches a task: plan → gated execution
→ review → restart, streamed over the cloud_gateways control channel. See
docs/browser-agent-architecture.md.
"""

import asyncio
import logging
import uuid

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from livellm_agent import __version__
from livellm_agent.config import settings
from livellm_agent.control import ControlChannel
from livellm_agent.models import Task, TaskMode
from livellm_agent.runner import Runner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="livellm-agent", version=__version__)

# One active task per agent pod (matches BrowserAgent.status.activeTaskId).
_active: dict[str, object] = {"task_id": None, "runner": None, "control": None}


class ActRequest(BaseModel):
    prompt: str
    mode: TaskMode = TaskMode.review


@app.get("/health/ping", tags=["Health"])
async def ping() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready", tags=["Health"])
async def ready() -> dict:
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
        "active_task": _active["task_id"],
    }


@app.post("/act", tags=["Agent"])
async def act(body: ActRequest) -> dict:
    """Create a task and start it. The UI follows it over the control channel."""
    if _active["task_id"] is not None:
        raise HTTPException(status_code=409, detail="agent already has an active task")
    if not settings.control_url:
        raise HTTPException(status_code=503, detail="control channel not configured")

    task = Task(
        id=str(uuid.uuid4()),
        tenant_id=settings.tenant_id,
        browser_agent_ref=settings.browser_agent_ref,
        prompt=body.prompt,
        mode=body.mode,
    )
    control = ControlChannel(settings.control_url, settings.control_token)
    await control.connect()
    runner = Runner(task, control)

    async def _drive() -> None:
        try:
            await runner.run()
        finally:
            await control.close()
            _active.update(task_id=None, runner=None, control=None)

    _active.update(task_id=task.id, runner=runner, control=control)
    asyncio.create_task(_drive())
    return {"task_id": task.id, "status": task.status.value}


if __name__ == "__main__":
    logger.info("livellm-agent %s starting on %s:%d (target=%s)",
                __version__, settings.host, settings.port,
                "controller" if settings.uses_controller else settings.cdp_ws_url or "unset")
    uvicorn.run(app, host=settings.host, port=settings.port)
