"""Typed event schema - the audit spine (guardrail layer L6).

Every consequential thing that happens in a run becomes one of these. The event log is
append-only: corrections are new events referencing a superseded id, never edits
(G-AUD-02). The writer API has no update or delete, so this is structural rather than
a convention.

Event types are deliberately granular. "Something went wrong" is not an event type;
``capture.frame_frozen`` and ``verifier.evaluation_failed`` are, because a report that
cannot distinguish them cannot tell a developer whether the game broke or the harness
did (G-AUD-06).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """Stable event taxonomy. Values are part of the report contract - do not rename."""

    # run lifecycle
    RUN_STARTED = "run.started"
    RUN_STATE_CHANGED = "run.state_changed"
    RUN_FINISHED = "run.finished"

    # policy / authorisation
    POLICY_CHECKED = "policy.checked"
    POLICY_REFUSED = "policy.refused"

    # perception
    PERCEPTION_OBSERVED = "perception.observed"
    PERCEPTION_DEGRADED = "perception.degraded"
    CAPTURE_FRAME = "capture.frame"
    CAPTURE_LOST = "capture.lost"
    CAPTURE_BLACK = "capture.black"
    CAPTURE_FROZEN = "capture.frozen"
    CAPTURE_UNSUPPORTED = "capture.unsupported"
    DISPLAY_CHANGED = "display.changed"
    WINDOW_FOUND = "window.found"
    WINDOW_LOST = "window.lost"
    WINDOW_IDENTITY_CHANGED = "window.identity_changed"
    OCR_COMPLETED = "ocr.completed"
    OCR_UNAVAILABLE = "ocr.unavailable"

    # planning
    PLAN_PROPOSED = "plan.proposed"
    PLAN_REJECTED = "plan.rejected"
    PLAN_VALIDATED = "plan.validated"
    PLANNER_DEGRADED = "planner.degraded"
    PLANNER_CALL = "planner.call"

    # execution
    ACTION_COMPILED = "action.compiled"
    ACTION_EXECUTED = "action.executed"
    ACTION_BLOCKED = "action.blocked"
    ACTION_SUPPRESSED = "action.suppressed"

    # verification
    VERIFIER_EVALUATED = "verifier.evaluated"
    VERIFIER_UNKNOWN = "verifier.unknown"
    ASSERTION_RESULT = "assertion.result"

    # safety
    ESTOP_TRIGGERED = "estop.triggered"
    FOCUS_LOST = "focus.lost"
    FOCUS_RESTORED = "focus.restored"
    HUMAN_INPUT_DETECTED = "human_input.detected"
    SESSION_INACTIVE = "session.inactive"
    BUDGET_CONSUMED = "budget.consumed"
    BUDGET_EXCEEDED = "budget.exceeded"

    # recovery
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_ATTEMPT = "recovery.attempt"
    RECOVERY_ESCALATED = "recovery.escalated"
    RECOVERY_GAVE_UP = "recovery.gave_up"

    # guardrails
    INJECTION_SUSPECTED = "guardrail.injection_suspected"
    HOSTILE_PLAN = "guardrail.hostile_plan"
    REDACTION_APPLIED = "guardrail.redaction_applied"


class Event(BaseModel):
    """One immutable audit record.

    ``data`` is intentionally free-form: specific event helpers in
    :mod:`frameforge.store.events` attach typed payloads while this class guarantees
    the envelope (ordering, timestamps, redaction) is uniform.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=0, description="Monotonic per-run sequence number.")
    kind: EventKind
    mono_ms: float = Field(description="Monotonic ms; durations are computed from this.")
    wall_ms: float = Field(description="Unix ms; for human-facing timelines only.")
    component: str = Field(description="Emitting component id, e.g. 'director', 'ocr.winrt'.")
    run_id: str
    summary: str = Field(max_length=400)
    data: dict[str, Any] = Field(default_factory=dict)
    supersedes: int | None = Field(
        default=None,
        description="seq of a prior event this one corrects. Never mutate the original.",
    )


class PolicyVerdict(BaseModel):
    """Outcome of an authorisation check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str
    rule_id: str | None = None
    warnings: tuple[str, ...] = ()


class Disposition(StrEnum):
    """Verification outcomes.

    ``UNKNOWN`` is a first-class, expected result. There is deliberately no code path
    that coerces it to PASS (guardrail G-VERD-02).
    """

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


DispositionLiteral = Literal["pass", "fail", "unknown"]

__all__ = ["Disposition", "DispositionLiteral", "Event", "EventKind", "PolicyVerdict"]