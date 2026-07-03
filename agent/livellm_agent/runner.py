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
    NEED_HUMAN_PREFIX,
    ModelNotConfigured,
    build_controller_tools,
    build_llm,
    build_session,
    resolve_use_vision,
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
from livellm_agent.planner import plan, replan_or_ask, synthesize

logger = logging.getLogger(__name__)

# Auto-mode failure policy: at most this many automatic replans per pass; past
# the cap a failed step always escalates to the human instead.
MAX_AUTO_REPLANS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _need_human(output: Optional[str]) -> Optional[str]:
    """The question when a sub-goal's final output is a NEED_HUMAN escape."""
    if not output:
        return None
    txt = output.strip().strip("\"'")
    if txt.startswith(NEED_HUMAN_PREFIX):
        return txt[len(NEED_HUMAN_PREFIX):].strip() or "The agent needs your input."
    return None


class Runner:
    def __init__(self, task: Task, control: ControlChannel):
        self.task = task
        self.control = control
        self.steps: list[Step] = []
        self._version = 1
        self._restart_to: Optional[int] = None
        self._restart = asyncio.Event()
        self._llm = None
        self._use_vision = "auto"
        self._replans = 0  # automatic replans this pass (capped at MAX_AUTO_REPLANS)
        self._session = None
        self._tools = None
        self._ctrl_client = None
        control.on_restart = self._on_restart
        control.on_control = self._on_control

    # ── control hooks ────────────────────────────────────────────────────
    async def _on_restart(self, step_idx: int) -> None:
        self._restart_to = max(0, min(step_idx, len(self.steps) - 1))
        self._restart.set()

    async def _on_control(self, op: str) -> None:
        # Reflect pause/resume in the task status immediately, so the UI sees
        # the control take effect (the engine bridge pauses the live agent).
        if op == "pause" and self.task.status == TaskStatus.running:
            self.task.status = TaskStatus.paused
            await self.control.emit("task.event", self.task.trajectory or {},
                                    status=TaskStatus.paused.value, message="paused by user")
        elif op == "resume" and self.task.status == TaskStatus.paused:
            self.task.status = TaskStatus.running
            await self.control.emit("task.event", self.task.trajectory or {},
                                    status=TaskStatus.running.value, message="resumed by user")

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

        self._use_vision = resolve_use_vision(self._llm, settings)
        self._session = build_session(settings)
        self._tools, self._ctrl_client = build_controller_tools(settings)

        try:
            await self._plan()
            start = 0
            while True:
                await self._execute_from(start)
                logger.info("task %s: pass finished (cancelled=%s)",
                            self.task.id, self.control.cancelled.is_set())
                if self.control.cancelled.is_set():
                    # cancelled mid-pass: mark and report as such — do NOT run
                    # _finish_pass (which would label it done/failed and spend
                    # an LLM call on synthesis).
                    self.task.status = TaskStatus.cancelled
                    await self.control.emit("run.done", self.task.trajectory or {},
                                            status=TaskStatus.cancelled.value,
                                            message="task cancelled")
                    return
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
        except asyncio.CancelledError:
            # browser-use's stop() cancels its internal step task and the
            # CancelledError (a BaseException — `except Exception` never sees
            # it) escapes here. Our own task isn't being cancelled, so
            # awaiting the emit is safe.
            status = TaskStatus.cancelled if self.control.cancelled.is_set() else TaskStatus.failed
            self.task.status = status
            logger.info("task %s: run stopped (%s)", self.task.id, status.value)
            await self.control.emit(
                "run.done", self.task.trajectory or {}, status=status.value,
                message="task cancelled" if status is TaskStatus.cancelled else "aborted",
            )
        except Exception as e:  # noqa: BLE001 — the task row must ALWAYS settle
            # A mid-step stop (cancel) or any engine error raises out of
            # _execute_from; without this the run ends with no run.done and
            # tenant-api shows "running" forever.
            status = TaskStatus.cancelled if self.control.cancelled.is_set() else TaskStatus.failed
            self.task.status = status
            logger.exception("task %s: run aborted", self.task.id)
            await self.control.emit(
                "run.done", self.task.trajectory or {}, status=status.value,
                message="task cancelled" if status is TaskStatus.cancelled else f"aborted: {e}",
            )
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
        self._replans = 0  # the replan cap is per pass
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
                self._session, self._llm, self._tools, step.intent, self._should_stop,
                control=self.control,
                use_vision=self._use_vision,
                llm_timeout=settings.llm_timeout,
            )
        except Exception as e:
            logger.exception("step %d failed", step.idx)
            step.status = StepStatus.failed
            step.action_json = {"output": None, "success": False, "error": str(e)}
            step.ended_at = _now()
            await self._emit_step(step, "failed")
            if self.task.mode.value != "review":
                return await self._on_step_failure(step)
            return "next"

        step.action_json = {"output": res.output, "success": res.success, "error": res.error}
        # capture exact browser state for restart-from-step (best-effort);
        # falls back to just the URL if the CDP snapshot fails.
        try:
            step.checkpoint_json = await checkpoint.snapshot(self._session)
        except Exception:
            step.checkpoint_json = {"url": res.url}
        step.screenshot_ref = self._save_screenshot(step, res.screenshot_b64)
        step.ended_at = _now()

        # ask-human escape hatch: the model ended the sub-goal with a
        # NEED_HUMAN question (credentials / 2FA / a choice it must not guess)
        question = _need_human(res.output)
        if question is not None:
            return await self._ask_human(step, question)

        step.status = StepStatus.done if res.success else StepStatus.failed
        await self._emit_step(step, "done" if res.success else "failed")

        if self.task.mode.value != "review":
            if step.status == StepStatus.failed:
                # never march blindly into dependent steps: replan the
                # remainder or escalate to the human (see _on_step_failure)
                return await self._on_step_failure(step)
            return "next"

        # block for a human verdict
        step.status = StepStatus.review
        await self._emit_step(step, "review")
        try:
            verdict = await self.control.wait_verdict(step.idx)
        except Exception:
            return "next"  # cancelled/closed
        return await self._apply_verdict(step, verdict)

    async def _ask_human(self, step: Step, question: str) -> str:
        """Surface a model question to the human and block for an answer.

        Reuses the existing plumbing (no new endpoints): the task parks as
        `paused` with the question in trajectory.result (NEED_HUMAN: …) and the
        step in `review`; a `rewrite` verdict's note (or action.intent) is the
        answer — it is injected as context and the step re-runs. Any other
        verdict behaves as usual.
        """
        step.status = StepStatus.review
        self.task.status = TaskStatus.paused
        if self.task.trajectory:
            self.task.trajectory.result = f"{NEED_HUMAN_PREFIX} {question}"
        await self._emit_step(step, f"needs human input: {question}")
        try:
            verdict = await self.control.wait_verdict(step.idx)
        except Exception:
            return "next"  # cancelled/closed
        self.task.status = TaskStatus.running
        if self.task.trajectory:
            self.task.trajectory.result = None
        if verdict.kind == VerdictKind.rewrite:
            answer = (verdict.action_json or {}).get("intent") or verdict.note
            if answer:
                step.intent = f"{step.intent}\n\nHuman-provided input: {answer}"
            step.status = StepStatus.pending
            return "retry"
        return await self._apply_verdict(step, verdict)

    async def _on_step_failure(self, step: Step) -> str:
        """Auto-mode failure policy: never advance past a failed step blindly.

        Ask the planner to either (a) revise the remainder of the plan based on
        what succeeded, or (b) escalate one clear question to the human. At most
        MAX_AUTO_REPLANS automatic replans per pass; past the cap — or when the
        replan call itself fails without a question — the failure always routes
        into the _ask_human flow (task parks as paused, NEED_HUMAN in
        trajectory.result, resumed by a rewrite verdict carrying the answer).
        """
        err = (step.action_json or {}).get("error") or (step.action_json or {}).get("output") or "no details"
        fallback_q = (
            f"Step {step.idx + 1} failed ('{step.intent}'): {str(err)[:300]}. "
            "How should I proceed?"
        )
        if self._replans >= MAX_AUTO_REPLANS:
            logger.info("task %s: replan cap (%d) reached; escalating step %d to human",
                        self.task.id, MAX_AUTO_REPLANS, step.idx)
            return await self._ask_human(step, fallback_q)

        decision = None
        try:
            decision = await replan_or_ask(settings, self.task.prompt, self.steps, step)
        except Exception as e:  # noqa: BLE001 — degrade to asking the human
            logger.warning("replan_or_ask failed for step %d: %s", step.idx, e)

        if decision and decision.get("steps"):
            self._replans += 1
            await self._reform_tail(
                step, decision["steps"],
                f"replanned after step {step.idx} failed "
                f"(v{self._version + 1}, replan {self._replans}/{MAX_AUTO_REPLANS})",
            )
            return "reform"
        question = (decision or {}).get("need_human") or fallback_q
        return await self._ask_human(step, question)

    async def _reform_tail(self, step: Step, intents: list[str], message: str) -> None:
        """Replace the plan from `step` onward with `intents` → new trajectory
        version, emit plan.ready. Shared by the reform verdict and auto-replan."""
        self._version += 1
        tail = [Step(idx=step.idx + k, intent=t) for k, t in enumerate(intents)]
        self.steps = self.steps[: step.idx] + tail
        self.task.trajectory = Trajectory(version=self._version, plan=self.steps)
        await self.control.emit("plan.ready", self.task.trajectory, status=self.task.status.value,
                                message=message)

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
            await self._reform_tail(step, intents, f"reformed trajectory (v{self._version + 1})")
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

        # Synthesize a final ANSWER to the task from the collected step outputs —
        # the run's deliverable. Best-effort; runs even if some steps failed (uses
        # whatever was gathered).
        notes = "\n\n".join(
            f"[{s.idx + 1}] {s.intent}\n{(s.action_json or {}).get('output') or '(no output)'}"
            for s in self.steps
            if (s.action_json or {}).get("output")
        )
        if self.task.trajectory and notes:
            try:
                answer = await synthesize(settings, self.task.prompt, notes)
                if answer:
                    self.task.trajectory.result = answer
            except Exception as e:
                logger.warning("final synthesis failed: %s", e)

        trajectory_ref = ""
        if settings.artifact_endpoint and self.task.trajectory:
            try:
                trajectory_ref = artifacts.store().put_trajectory(self.task.id, self.task.trajectory)
            except Exception as e:
                logger.warning("trajectory upload failed: %s", e)

        result = (self.task.trajectory.result if self.task.trajectory else "") or ""
        msg = f"task {self.task.status.value}"
        if result:
            msg += "\n\n" + result
        # video_ref stays empty until the recorder lands (deferred) — see docs.
        await self.control.emit(
            "run.done", self.task.trajectory, status=self.task.status.value,
            message=msg, video_ref="", trajectory_ref=trajectory_ref or "",
        )
