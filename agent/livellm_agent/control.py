"""Control channel — HTTP, tenant-api-centric.

The platform convention is that the UI talks only to tenant-api, so the agent's
control plane is HTTP, not a gateway WebSocket:

- OUTBOUND: the agent POSTs trajectory SNAPSHOTS to a per-task `callback_url`
  (tenant-api's /v1/internal/browser-agent/events), which persists them and
  fans out webhooks. Each emit sends the full current trajectory + status, so
  tenant-api always has the latest per-step state.
- INBOUND: tenant-api forwards UI actions (verdict / pause-resume-cancel /
  restart) to the agent's HTTP endpoints, whose handlers call submit_* here.
  Those feed the same per-step Future / pause Event the runner awaits, so the
  runner loop is unchanged.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel

from livellm_agent.models import Verdict

logger = logging.getLogger(__name__)


class ControlChannel:
    def __init__(self, callback_url: Optional[str], task_id: str, tenant: Optional[str]):
        self._callback = callback_url
        self._task_id = task_id
        self._tenant = tenant
        self._http = httpx.AsyncClient(timeout=15.0)

        # runner awaits a verdict for the step it just executed
        self._verdict_futures: dict[int, asyncio.Future[Verdict]] = {}

        # control flags the runner polls / awaits
        self.paused = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()

        # hook the runner sets to handle a restart-from-step request
        self.on_restart: Optional[Callable[[int], Awaitable[None]]] = None

    async def close(self) -> None:
        await self._http.aclose()

    # ── outbound: trajectory snapshots → tenant-api ─────────────────────────
    async def emit(
        self,
        event_type: str,
        trajectory,
        status: str,
        message: str = "",
        video_ref: str = "",
        trajectory_ref: str = "",
    ) -> None:
        if not self._callback:
            return  # standalone / no callback configured
        traj = trajectory.model_dump(mode="json") if isinstance(trajectory, BaseModel) else (trajectory or {})
        payload = {
            "task_id": self._task_id,
            "tenant": self._tenant,
            "type": event_type,
            "status": status,
            "trajectory": traj,
            "video_ref": video_ref,
            "trajectory_ref": trajectory_ref,
            "message": message,
        }
        try:
            await self._http.post(self._callback, json=payload)
        except Exception as e:  # reporting is best-effort; never fail the run
            logger.warning("control: emit %s failed: %s", event_type, e)

    # ── inbound: called by the agent's HTTP handlers ────────────────────────
    def submit_verdict(self, v: Verdict) -> None:
        fut = self._verdict_futures.get(v.step_idx)
        if fut and not fut.done():
            fut.set_result(v)

    def submit_control(self, op: str) -> None:
        if op == "pause":
            self.paused.set()
            self._resume.clear()
        elif op == "resume":
            self.paused.clear()
            self._resume.set()
        elif op == "cancel":
            self.cancelled.set()
            self._resume.set()
            self._fail_pending(RuntimeError("cancelled"))

    def submit_restart(self, step_idx: int) -> None:
        if self.on_restart:
            asyncio.create_task(self.on_restart(step_idx))

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._verdict_futures.values():
            if not fut.done():
                fut.set_exception(exc)

    # ── runner helpers ───────────────────────────────────────────────────
    async def wait_verdict(self, step_idx: int) -> Verdict:
        """Block until a verdict for this step arrives (or cancel)."""
        fut: asyncio.Future[Verdict] = asyncio.get_event_loop().create_future()
        self._verdict_futures[step_idx] = fut
        try:
            return await fut
        finally:
            self._verdict_futures.pop(step_idx, None)

    async def gate(self) -> None:
        """Honor a pause requested from the UI; raises if cancelled."""
        if self.cancelled.is_set():
            raise RuntimeError("cancelled")
        if self.paused.is_set():
            logger.info("control: paused, awaiting resume")
            await self._resume.wait()
        if self.cancelled.is_set():
            raise RuntimeError("cancelled")
