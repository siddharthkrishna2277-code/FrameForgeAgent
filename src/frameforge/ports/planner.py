"""Planner port - the one slot in the loop that AI is allowed to fill.

A planner is handed an immutable ``PlanRequest`` and returns a ``PlanProposal``. It has
no ability to execute, no access to the input port, and no way to express "just send
the keystroke". The most it can do is name logical actions, which then face the
validator, the compiler, the budget ledger, the focus guard and the executor.

Three implementations ship:

* ``ProfilePlanner``  - deterministic. Anchor graph, reaction policies, task steps.
* ``AiPlanner``      - optional remote reasoning behind a provider adapter.
* ``RecordedPlanner`` - replays a recorded plan. This is what makes AI behaviour
  deterministically testable and reproducible.

Because all three satisfy the same contract, ``ai: off`` is a first-class mode rather
than a degraded one (guardrail G-DEG-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from frameforge.actions.model import Action
from frameforge.kernel.events import PolicyVerdict


class PlannerTier(StrEnum):
    """Which tier produced a decision. Recorded in every event for the report."""

    TIER0_PROFILE = "tier0_profile"
    TIER1_AI = "tier1_ai"
    TIER1_RECORDED = "tier1_recorded"

    @property
    def is_ai(self) -> bool:
        return self is not PlannerTier.TIER0_PROFILE


@dataclass(frozen=True, slots=True)
class AnchorObservation:
    """One visual landmark's current score, as seen by the planner.

    Deliberately just a number plus a location. A planner that receives raw frames from
    a local tier would start to guess at pixels; passing scores keeps Tier 0 deterministic
    and keeps the AI packet small (guardrail G-PER-01).
    """

    name: str
    score: float
    rect: tuple[int, int, int, int] | None = None
    center: tuple[int, int] | None = None
    threshold: float = 0.8

    @property
    def present(self) -> bool:
        return self.score >= self.threshold


@dataclass(frozen=True, slots=True)
class PerceptionSummary:
    """The distilled state a planner may reason about.

    Built by the assembler from frame + window identity + OCR + anchors + health. This
    is the *only* perception surface a planner sees, and for a remote planner it is
    exactly what gets serialised (after redaction).
    """

    mono_ms: float
    frame_index: int
    frame_hash: str
    surface_size: tuple[int, int]
    window_title: str = ""
    window_class: str = ""
    process_name: str = ""
    is_foreground: bool = True
    anchors: tuple[AnchorObservation, ...] = ()
    ocr_lines: tuple[tuple[str, tuple[int, int, int, int], float], ...] = ()
    color_probes: tuple[tuple[str, bool, float], ...] = ()
    region_activity: float = 0.0
    health: str = "ok"
    confidence: float = 0.0
    notes: tuple[str, ...] = ()

    def anchor(self, name: str) -> AnchorObservation | None:
        return next((a for a in self.anchors if a.name == name), None)

    def anchor_present(self, name: str) -> bool:
        a = self.anchor(name)
        return bool(a and a.present)

    def present_anchors(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.anchors if a.present)

    def to_text_lines(self, max_ocr: int = 20) -> list[str]:
        """Compact text rendering for a text-only AI model."""
        out = [f"screen={self.surface_size[0]}x{self.surface_size[1]}"]
        out.append(f"window={self.window_title!r} class={self.window_class!r} fg={self.is_foreground}")
        if self.anchors:
            out.append(
                "anchors=" + ", ".join(f"{a.name}:{a.score:.2f}{'*' if a.present else ''}" for a in self.anchors)
            )
        if self.color_probes:
            out.append("probes=" + ", ".join(f"{n}={'y' if ok else 'n'}" for n, ok, _ in self.color_probes))
        if self.ocr_lines:
            out.append("text:")
            for text, _rect, conf in self.ocr_lines[:max_ocr]:
                out.append(f"  - {text!r} ({conf:.2f})")
        out.append(f"activity={self.region_activity:.3f} health={self.health}")
        return out


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One proposed step: an action plus how it expects to be checked."""

    action: Action
    expected_change: str | None = None
    rationale: str = ""
    confidence: float | None = None  # untrusted annotation only; never gates (G-DEC-04)

    def describe(self) -> str:
        return self.action.describe()


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Objective, history and constraints handed to a planner."""

    objective: str
    allowed_actions: tuple[str, ...] = ()
    allowed_intents: tuple[str, ...] = ()
    current_step: str | None = None
    step_index: int = 0
    total_steps: int = 0
    recent_transitions: tuple[str, ...] = ()
    recent_failures: tuple[str, ...] = ()
    budget_actions_left: int = 0
    budget_ai_calls_left: int = 0
    attempts_at_step: int = 0
    recovery_mode: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything a planner may see. Nothing else is passed, ever."""

    context: PlanContext
    perception: PerceptionSummary
    request_id: str = ""

    def packet_lines(self, max_ocr: int = 20) -> list[str]:
        """The perception packet, as text. Redacted upstream by the packet builder."""
        c, p = self.context, self.perception
        out = [f"objective: {c.objective}"]
        if c.current_step:
            out.append(f"current_step[{c.step_index + 1}/{c.total_steps}]: {c.current_step}")
        if c.allowed_intents:
            out.append("allowed_intents: " + ", ".join(c.allowed_intents))
        out.append(f"budget: actions_left={c.budget_actions_left} ai_calls_left={c.budget_ai_calls_left}")
        if c.attempts_at_step:
            out.append(f"attempts_at_this_step: {c.attempts_at_step}")
        if c.recent_failures:
            out.append("recent_failures: " + " | ".join(c.recent_failures[-3:]))
        if c.recent_transitions:
            out.append("recent_transitions: " + " -> ".join(c.recent_transitions[-6:]))
        out.append("--- screen state ---")
        out.extend(p.to_text_lines(max_ocr))
        return out


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A planner's answer. Always a proposal - never an execution."""

    steps: tuple[PlanStep, ...] = ()
    tier: PlannerTier = PlannerTier.TIER0_PROFILE
    planner_id: str = ""
    rationale: str = ""
    #: Set when the planner is declining to act. ``UNKNOWN`` must be expressible.
    abstain: bool = False
    abstain_reason: str = ""
    degraded: bool = False
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.abstain


@dataclass(frozen=True, slots=True)
class PlannerCapabilities:
    name: str
    tier: PlannerTier
    available: bool
    requires_network: bool = False
    notes: tuple[str, ...] = ()


@runtime_checkable
class PlannerPort(Protocol):
    """Proposes the next logical action(s). Cannot execute anything."""

    name: str

    def propose(self, request: PlanRequest) -> PlanProposal:
        """Return a proposal. Must not raise for ordinary failure."""
        ...

    def capabilities(self) -> PlannerCapabilities:
        ...


__all__ = [
    "AnchorObservation",
    "PerceptionSummary",
    "PlanContext",
    "PlanProposal",
    "PlanRequest",
    "PlanStep",
    "PlannerCapabilities",
    "PlannerPort",
    "PlannerTier",
    "PolicyVerdict",
]
