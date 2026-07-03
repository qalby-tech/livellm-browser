"""livellm-agent entrypoint.

Boots a small FastAPI surface so the pod is healthy as soon as it starts, even
before a task is assigned. tenant-api drives the agent over HTTP:
  POST /act       launch a task (tenant-api passes our task_id + a callback_url
                  it wants trajectory snapshots posted to)
  POST /verdict   per-step review verdict (done/not_done/reform/rewrite)
  POST /control   pause | resume | cancel
  POST /restart   restart-from-step
See docs/browser-agent-architecture.md.
"""

import asyncio
import logging
import uuid
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from livellm_agent import __version__
from livellm_agent.config import settings
from livellm_agent.control import ControlChannel
from livellm_agent.models import Task, TaskMode, Verdict, VerdictKind
from livellm_agent.runner import Runner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="livellm-agent", version=__version__)

# One active task per agent pod (matches BrowserAgent.status.activeTaskId).
_active: dict[str, Any] = {"task_id": None, "runner": None, "control": None, "drive": None}


class ActRequest(BaseModel):
    prompt: str
    mode: TaskMode = TaskMode.review
    task_id: Optional[str] = None       # tenant-api's task id (shared); generated if absent
    tenant: Optional[str] = None
    callback_url: Optional[str] = None  # where to POST trajectory snapshots


class VerdictRequest(BaseModel):
    task_id: str
    step_idx: int
    kind: VerdictKind
    note: Optional[str] = None
    action: Optional[dict] = None


class ControlRequest(BaseModel):
    task_id: str
    op: str  # pause | resume | cancel


class RestartRequest(BaseModel):
    task_id: str
    step_idx: int


def _control_for(task_id: str) -> ControlChannel:
    if _active["task_id"] != task_id or _active["control"] is None:
        raise HTTPException(status_code=404, detail="no such active task")
    return _active["control"]


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
    """Create a task and start it. tenant-api follows it via the callback."""
    if _active["task_id"] is not None:
        raise HTTPException(status_code=409, detail="agent already has an active task")

    task_id = body.task_id or str(uuid.uuid4())
    tenant = body.tenant or settings.tenant_id
    task = Task(
        id=task_id, tenant_id=tenant,
        browser_agent_ref=settings.browser_agent_ref,
        prompt=body.prompt, mode=body.mode,
    )
    control = ControlChannel(body.callback_url, task_id, tenant)
    runner = Runner(task, control)

    async def _drive() -> None:
        try:
            await runner.run()
        finally:
            await control.close()
            _active.update(task_id=None, runner=None, control=None, drive=None)

    _active.update(task_id=task_id, runner=runner, control=control)
    # keep a strong reference: asyncio only weak-refs tasks, and a GC'd drive
    # task silently kills the run (and with it, pause/cancel handling)
    _active["drive"] = asyncio.create_task(_drive())
    return {"task_id": task_id, "status": task.status.value}


@app.post("/verdict", tags=["Agent"])
async def verdict(body: VerdictRequest) -> dict:
    _control_for(body.task_id).submit_verdict(
        Verdict(step_idx=body.step_idx, kind=body.kind, note=body.note, action_json=body.action)
    )
    return {"ok": True}


@app.post("/control", tags=["Agent"])
async def control(body: ControlRequest) -> dict:
    if body.op not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="op must be pause|resume|cancel")
    _control_for(body.task_id).submit_control(body.op)
    return {"ok": True}


@app.post("/restart", tags=["Agent"])
async def restart(body: RestartRequest) -> dict:
    _control_for(body.task_id).submit_restart(body.step_idx)
    return {"ok": True}


if __name__ == "__main__":
    logger.info("livellm-agent %s starting on %s:%d (target=%s)",
                __version__, settings.host, settings.port,
                "controller" if settings.uses_controller else settings.cdp_ws_url or "unset")
    uvicorn.run(app, host=settings.host, port=settings.port)
