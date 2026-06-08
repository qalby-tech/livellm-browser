"""Data model for tasks, trajectories, steps and verdicts.

These mirror the browser_agent_tasks shape owned by tenant-api (the trajectory
is persisted as a JSONB snapshot). The agent runtime is stateless about durable
storage — it emits trajectory snapshots; tenant-api persists them.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── enums ─────────────────────────────────────────────────────────────────

class TaskMode(str, Enum):
    auto = "auto"        # run to completion, review only on request
    review = "review"    # pause for a verdict on every step


class TaskStatus(str, Enum):
    planning = "planning"
    running = "running"
    paused = "paused"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    review = "review"     # executed, blocked awaiting a verdict
    done = "done"
    skipped = "skipped"
    failed = "failed"


class VerdictKind(str, Enum):
    done = "done"             # accept, continue
    not_done = "not_done"     # retry this step
    reform = "reform"         # re-plan from here → new trajectory version
    rewrite = "rewrite"       # replace this step's action, then run it


# ── core entities ───────────────────────────────────────────────────────────

class Step(BaseModel):
    idx: int = Field(..., description="0-based position in the trajectory")
    intent: str = Field(..., description="Natural-language goal of this step")
    action_json: Optional[dict[str, Any]] = Field(
        default=None, description="Concrete action (filled at/after execution)"
    )
    status: StepStatus = StepStatus.pending
    checkpoint_json: Optional[dict[str, Any]] = Field(
        default=None, description="CDP state snapshot (url/cookies/localStorage) for restart-from-step"
    )
    screenshot_ref: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class Trajectory(BaseModel):
    version: int = Field(default=1, description="Bumped on each reform")
    plan: list[Step] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class Task(BaseModel):
    id: str
    tenant_id: Optional[str] = None
    browser_agent_ref: Optional[str] = None
    prompt: str
    mode: TaskMode = TaskMode.review
    status: TaskStatus = TaskStatus.planning
    trajectory: Optional[Trajectory] = None
    created_at: datetime = Field(default_factory=_now)


class Verdict(BaseModel):
    step_idx: int
    kind: VerdictKind
    note: Optional[str] = None
    action_json: Optional[dict[str, Any]] = Field(
        default=None, description="Replacement action when kind == rewrite"
    )
    actor: Optional[str] = None
    at: datetime = Field(default_factory=_now)


# Control plane is HTTP (tenant-api-centric), not a WS protocol:
#  - OUTBOUND: the runner emits trajectory SNAPSHOTS via control.emit(type, …) →
#    POSTed to tenant-api's callback (event types: plan.ready / step.event /
#    run.done). tenant-api persists the snapshot + fans out webhooks.
#  - INBOUND: tenant-api forwards UI actions to the agent's /verdict, /control,
#    /restart endpoints, which build a Verdict / op / step_idx and hand it to the
#    ControlChannel. No typed wire-message classes are needed.
