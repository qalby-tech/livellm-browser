"""Task runner — orchestrates a task end to end.

plan → emit trajectory → for each sub-goal: gate (pause/cancel) → execute →
checkpoint → (in review mode) emit for review and block on the verdict →
apply the verdict (done / not-done / reform / rewrite) → advance. After the
pass, emit run.done and persist the trajectory; a restart-from-step request
re-executes from the chosen step.

The runner is stateless about durable storage — it streams events over the
control channel (tenant-api persists them) and uploads artifacts to MinIO.
"""

import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Optional

from livellm_agent import artifacts, checkpoint
from livellm_agent.config import settings
from livellm_agent.control import ControlChannel
from livellm_agent.engine import (
    ModelNotConfigured,
    build_controller_tools,
    build_llm,
    build_session,
    run_subgoal,
)
from livellm_agent.models import (
    Step,
    StepStatus,
    Task,
    TaskStatus,
    Trajectory,
    Verdict,
    VerdictKind,
)
from livellm_agent.planner import plan

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Runner:
    def __init__(self, task: Task, control: ControlChannel):
        self.task = task
        self.control = control
        self.steps: list[Step] = []
        self._version = 1
        self._restart_to: Optional[int] = None
        self._restart = asyncio.Event()
        self._llm = None
        self._session = None
        self._tools = None
        self._ctrl_client = None
        control.on_restart = self._on_restart

    # ── control hooks ────────────────────────────────────────────────────
    async def _on_restart(self, step_idx: int) -> None:
        self._restart_to = max(0, min(step_idx, len(self.steps) - 1))
        self._restart.set()

    async def _should_stop(self) -> bool:
        # stop the current sub-goal cleanly on cancel or a restart request
        return self.control.cancelled.is_set() or self._restart.is_set()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def run(self) -> None:
        try:
            self._llm = build_llm(settings)
        except ModelNotConfigured as e:
            logger.warning("task %s: %s", self.task.id, e)
            self.task.status = TaskStatus.failed
            await self.control.emit("run.done", {}, status=TaskStatus.failed.value,
                                    message="model_not_configured")
            return

        self._session = build_session(settings)
        self._tools, self._ctrl_client = build_controller_tools(settings)

        try:
            await self._plan()
            start = 0
            while True:
                await self._execute_from(start)
                await self._finish_pass()
                # wait for a post-completion restart, or stop
                self._restart.clear()
                waiters = [
                    asyncio.create_task(self._restart.wait()),
                    asyncio.create_task(self.control.cancelled.wait()),
                ]
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if self.control.cancelled.is_set():
                    break
                start = self._restart_to or 0
                self._restart.clear()
        finally:
            if self._ctrl_client:
                await self._ctrl_client.close()

    async def _plan(self) -> None:
        self.task.status = TaskStatus.planning
        intents = await plan(settings, self.task.prompt)
        self.steps = [Step(idx=i, intent=t) for i, t in enumerate(intents)]
        self.task.trajectory = Trajectory(version=self._version, plan=self.steps)
        await self.control.emit("plan.ready", self.task.trajectory, status=self.task.status.value,
                                message=f"planned {len(self.steps)} step(s)")

    # ── execution ──────────────────────────────────────────────────────────
    async def _execute_from(self, i: int) -> None:
        self.task.status = TaskStatus.running
        # reset statuses from the start index forward
        for s in self.steps[i:]:
            s.status = StepStatus.pending
        # restart-from-step: restore the state captured AFTER the prior step so
        # step i re-runs from the same browser state it originally saw.
        if i > 0 and self.steps[i - 1].checkpoint_json:
            await checkpoint.restore(self._session, self.steps[i - 1].checkpoint_json)
        while i < len(self.steps):
            if self._restart.is_set():
                return  # outer loop will re-enter at _restart_to
            step = self.steps[i]
            try:
                await self.control.gate()  # honor pause; raises on cancel
            except RuntimeError:
                return  # cancelled

            advance = await self._run_step(step)
            if advance == "retry" or advance == "reform":
                continue  # i unchanged
            i += 1

    async def _emit_step(self, step: Step, phase: str) -> None:
        await self.control.emit(
            "step.event", self.task.trajectory, status=self.task.status.value,
            message=f"step {step.idx} {phase}: {step.intent}",
        )

    async def _run_step(self, step: Step) -> str:
        step.status = StepStatus.running
        step.started_at = _now()
        await self._emit_step(step, "started")

        try:
            res = await run_subgoal(
                self._session, self._llm, self._tools, step.intent, self._should_stop
            )
        except Exception:
            logger.exception("step %d failed", step.idx)
            step.status = StepStatus.failed
            step.ended_at = _now()
            await self._emit_step(step, "failed")
            return "next"

        step.action_json = {"output": res.output, "success": res.success}
        # capture exact browser state for restart-from-step (best-effort);
        # falls back to just the URL if the CDP snapshot fails.
        try:
            step.checkpoint_json = await checkpoint.snapshot(self._session)
        except Exception:
            step.checkpoint_json = {"url": res.url}
        step.screenshot_ref = self._save_screenshot(step, res.screenshot_b64)
        step.ended_at = _now()
        step.status = StepStatus.done if res.success else StepStatus.failed
        await self._emit_step(step, "done")

        if self.task.mode.value != "review":
            return "next"

        # block for a human verdict
        step.status = StepStatus.review
        await self._emit_step(step, "review")
        try:
            verdict = await self.control.wait_verdict(step.idx)
        except Exception:
            return "next"  # cancelled/closed
        return await self._apply_verdict(step, verdict)

    async def _apply_verdict(self, step: Step, v: Verdict) -> str:
        if v.kind == VerdictKind.done:
            step.status = StepStatus.done
            return "next"
        if v.kind == VerdictKind.not_done:
            step.status = StepStatus.pending
            return "retry"
        if v.kind == VerdictKind.rewrite:
            # replace the step's intent with the human-supplied action/note
            new_intent = (v.action_json or {}).get("intent") or v.note
            if new_intent:
                step.intent = str(new_intent)
            step.status = StepStatus.pending
            return "retry"
        if v.kind == VerdictKind.reform:
            # re-plan the remainder from here → new trajectory version
            ctx = f"{self.task.prompt}\n\nReplan from step {step.idx} ('{step.intent}'). Reviewer note: {v.note or ''}"
            intents = await plan(settings, ctx)
            self._version += 1
            tail = [Step(idx=step.idx + k, intent=t) for k, t in enumerate(intents)]
            self.steps = self.steps[: step.idx] + tail
            self.task.trajectory = Trajectory(version=self._version, plan=self.steps)
            await self.control.emit("plan.ready", self.task.trajectory, status=self.task.status.value,
                                    message=f"reformed trajectory (v{self._version})")
            return "reform"
        return "next"

    # ── artifacts / finish ───────────────────────────────────────────────
    def _save_screenshot(self, step: Step, b64: Optional[str]) -> Optional[str]:
        if not b64 or not settings.artifact_endpoint:
            return None
        try:
            data = base64.b64decode(b64)
            return artifacts.store().put_bytes(
                self.task.id, f"step-{step.idx}.jpg", data, "image/jpeg"
            )
        except Exception as e:
            logger.warning("screenshot upload failed: %s", e)
            return None

    async def _finish_pass(self) -> None:
        failed = any(s.status == StepStatus.failed for s in self.steps)
        self.task.status = TaskStatus.failed if failed else TaskStatus.done
        trajectory_ref = ""
        if settings.artifact_endpoint and self.task.trajectory:
            try:
                trajectory_ref = artifacts.store().put_trajectory(
                    self.task.id, self.task.trajectory
                )
            except Exception as e:
                logger.warning("trajectory upload failed: %s", e)
        # video_ref stays empty until the recorder lands (deferred) — see docs.
        await self.control.emit(
            "run.done", self.task.trajectory, status=self.task.status.value,
            message=f"task {self.task.status.value}", video_ref="", trajectory_ref=trajectory_ref or "",
        )
