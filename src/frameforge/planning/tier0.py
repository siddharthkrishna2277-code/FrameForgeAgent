"""Tier-0 planner - the deterministic policy engine.

This is the system. Not a placeholder standing in for a model: a complete agent composed
of four mechanisms, which together cover the overwhelming majority of real value (menu
navigation, QA scenarios, anchored interaction) with no network and no AI provider.

1. **Task steps** - an ordered objective with preconditions and postconditions.
2. **Reaction policies** - ordered if/then rules over the observation. Fires *before*
   task steps, because a modal dialog must be dismissed before anything else works.
3. **Anchor graph** - declared UI transitions (``main_menu -> settings -> back``),
   followed one edge at a time.
4. **Bounded probe** - when nothing applies, try declared candidates in a seeded order
   with a hard cap. Deterministic given a seed.

Every branch that finds nothing returns ``abstain``, which is a first-class answer
(guardrail G-DEC-05). A deterministic agent that must always emit an action would be
less safe, not more.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from frameforge.actions.model import (
    Action,
    Click,
    Intent,
    KeyPress,
    MoveMouse,
    Point,
    Scroll,
    Wait,
)
from frameforge.ports.input import Key
from frameforge.ports.planner import (
    PerceptionSummary,
    PlanContext,
    PlanProposal,
    PlanRequest,
    PlanStep,
    PlannerCapabilities,
    PlannerTier,
)

if TYPE_CHECKING:  # pragma: no cover
    from frameforge.tasks.dsl import TaskStep


@dataclass(slots=True)
class ReactionRule:
    """One ordered if/then rule.

    ``when`` is a small declarative predicate rather than code, so a profile author can
    add a game-specific reaction without writing Python. It deliberately supports only
    what is cheap and reliable: anchor presence, OCR regex, colour probe, confidence
    bands. It cannot express pixel arithmetic, which is the point - a rule that needed
    that would belong in code, with a test.
    """

    name: str
    when: dict[str, object]
    action: Action
    expected_change: str = ""
    priority: int = 0
    max_uses: int | None = None
    uses: int = 0

    def available(self) -> bool:
        return self.max_uses is None or self.uses < self.max_uses

    def matches(self, perception: PerceptionSummary) -> bool:
        w = self.when
        if "anchor" in w and not perception.anchor_present(str(w["anchor"])):
            return False
        if "anchor_absent" in w and perception.anchor_present(str(w["anchor_absent"])):
            return False
        if "anchor_below" in w:
            spec = w["anchor_below"]
            if not isinstance(spec, tuple | list) or len(spec) != 2:
                return False
            name, threshold = str(spec[0]), float(spec[1])
            a = perception.anchor(name)
            if a is None or a.score > threshold:
                return False
        if "text" in w:
            import re

            if not any(
                re.search(str(w["text"]), text, re.IGNORECASE)
                for text, _r, _c in perception.ocr_lines
            ):
                return False
        if "text_absent" in w:
            import re

            if any(
                re.search(str(w["text_absent"]), text, re.IGNORECASE)
                for text, _r, _c in perception.ocr_lines
            ):
                return False
        if "probe" in w:
            if not perception.health:
                return False
            expected = bool(w.get("probe_value", True))
            name = str(w["probe"])
            actual = any(n == name and ok for n, ok, _r in perception.color_probes)
            if actual is not expected:
                return False
        if "min_confidence" in w and perception.confidence < float(w["min_confidence"]):
            return False
        if "max_confidence" in w and perception.confidence > float(w["max_confidence"]):
            return False
        if "health" in w and perception.health != str(w["health"]):
            return False
        return True


@dataclass(slots=True)
class GraphEdge:
    """One declared UI transition: "if this anchor is showing, this action moves on"."""

    from_anchor: str
    action: Action
    expected_change: str = ""


@dataclass(slots=True)
class UiGraph:
    """Anchor transition graph for menu navigation."""

    name: str = "default"
    edges: list[GraphEdge] = field(default_factory=list)

    def next_action(self, perception: PerceptionSummary) -> GraphEdge | None:
        """First edge whose ``from_anchor`` is present. Order is author-controlled."""
        for edge in self.edges:
            if perception.anchor_present(edge.from_anchor):
                return edge
        return None


@dataclass(slots=True)
class ProbeSet:
    """Bounded, seeded exploration when nothing else applies."""

    candidates: list[Action] = field(default_factory=list)
    max_probes: int = 6
    seed: int = 0

    def ordered(self, attempt: int) -> list[Action]:
        """Deterministic probe order for a given attempt number."""
        if not self.candidates:
            return []
        rng = random.Random(self.seed + attempt)
        ordered = list(self.candidates)
        rng.shuffle(ordered)
        return ordered[: self.max_probes]


class ProfilePlanner:
    """Deterministic planner. No network, no model, fully reproducible."""

    def __init__(
        self,
        *,
        reactions: list[ReactionRule] | None = None,
        graph: UiGraph | None = None,
        probes: ProbeSet | None = None,
        name: str = "profile",
    ) -> None:
        self.reactions = sorted(reactions or [], key=lambda r: -r.priority)
        self.graph = graph or UiGraph()
        self.probes = probes or ProbeSet()
        self.name = name
        self.decisions = 0
        self.abstentions = 0
        self.reaction_fires: dict[str, int] = {}
        #: step name -> action. Instance state, populated by the task runner. Kept off the
        #: class deliberately: a class-level dict would be shared across planner instances
        #: and leak one run's step actions into the next.
        self._step_actions: dict[str, Action] = {}

    # ------------------------------------------------------------------ propose

    def propose(self, request: PlanRequest) -> PlanProposal:
        self.decisions += 1
        context = request.context
        perception = request.perception

        # Recovery mode: a probe set is the right answer, since we have already failed
        # the scripted path.
        if context.recovery_mode:
            probe = self._next_probe(context.attempts_at_step)
            if probe is not None:
                return PlanProposal(
                    steps=(PlanStep(action=probe, expected_change="state changes", rationale="recovery probe"),),
                    tier=PlannerTier.TIER0_PROFILE,
                    planner_id=self.name,
                    rationale="recovery probe",
                )

        # 1. Reactions first: a modal dialog blocks everything below it.
        reaction = self._match_reaction(perception)
        if reaction is not None:
            reaction.uses += 1
            self.reaction_fires[reaction.name] = self.reaction_fires.get(reaction.name, 0) + 1
            return PlanProposal(
                steps=(
                    PlanStep(
                        action=reaction.action,
                        expected_change=reaction.expected_change or f"reaction {reaction.name} resolves",
                        rationale=f"reaction rule {reaction.name!r} matched",
                    ),
                ),
                tier=PlannerTier.TIER0_PROFILE,
                planner_id=self.name,
                rationale=f"reaction:{reaction.name}",
            )

        # 2. A step that declares a UI anchor it needs.
        step_action = self._step_action(context)
        if step_action is not None:
            return PlanProposal(
                steps=(step_action,),
                tier=PlannerTier.TIER0_PROFILE,
                planner_id=self.name,
                rationale="task step action",
            )

        # 3. Graph edge.
        edge = self.graph.next_action(perception)
        if edge is not None:
            return PlanProposal(
                steps=(
                    PlanStep(
                        action=edge.action,
                        expected_change=edge.expected_change or f"advances past {edge.from_anchor}",
                        rationale=f"ui graph edge from {edge.from_anchor}",
                    ),
                ),
                tier=PlannerTier.TIER0_PROFILE,
                planner_id=self.name,
                rationale=f"graph:{edge.from_anchor}",
            )

        # 4. Probe, if permitted and in budget.
        if context.budget_actions_left == 0:
            return self._abstain("action budget exhausted")
        probe = self._next_probe(context.attempts_at_step)
        if probe is not None:
            return PlanProposal(
                steps=(PlanStep(action=probe, expected_change="state changes", rationale="bounded probe"),),
                tier=PlannerTier.TIER0_PROFILE,
                planner_id=self.name,
                rationale="bounded probe",
            )

        return self._abstain("no rule, step action, graph edge, or probe applies")

    def _abstain(self, reason: str) -> PlanProposal:
        """Declining is a valid, encouraged outcome (guardrail G-DEC-05)."""
        self.abstentions += 1
        return PlanProposal(
            steps=(),
            tier=PlannerTier.TIER0_PROFILE,
            planner_id=self.name,
            abstain=True,
            abstain_reason=reason,
            rationale=reason,
        )

    def _match_reaction(self, perception: PerceptionSummary) -> ReactionRule | None:
        for rule in self.reactions:
            if rule.available() and rule.matches(perception):
                return rule
        return None

    def _step_action(self, context: PlanContext) -> PlanStep | None:
        """Extract an action from a step's inline declaration.

        Task steps carry their action through a small registry keyed by name, populated
        by the runner. Keeping it as a lookup rather than a direct reference means the
        planner port stays free of task-DSL types.
        """
        if not context.current_step:
            return None
        action = self._step_actions.get(context.current_step)
        if action is None:
            return None
        return PlanStep(
            action=action,
            expected_change=f"step {context.current_step!r} progresses",
            rationale="declared task-step action",
        )

    def _next_probe(self, attempt: int) -> Action | None:
        if attempt >= self.probes.max_probes:
            return None
        ordered = self.probes.ordered(attempt)
        return ordered[attempt] if attempt < len(ordered) else None

    def bind_step_actions(self, mapping: dict[str, Action]) -> None:
        """Tell the planner what each named step should do. Instance-level, not class."""
        self._step_actions = dict(mapping)

    def capabilities(self) -> PlannerCapabilities:
        return PlannerCapabilities(
            name=self.name,
            tier=PlannerTier.TIER0_PROFILE,
            available=True,
            requires_network=False,
            notes=(
                f"reactions={len(self.reactions)}",
                f"graph_edges={len(self.graph.edges)}",
                f"probes={len(self.probes.candidates)}",
            ),
        )


class ScriptedPlanner:
    """Replays a fixed sequence. Used by tests and by ``frameforge simulate``.

    Also the shape a RecordedPlanner takes: a list of proposals consumed in order, so an
    AI-driven run can be replayed deterministically (guardrail G-DEG-03, stability
    criterion 11).
    """

    def __init__(self, proposals: list[PlanProposal], name: str = "scripted") -> None:
        self._proposals = list(proposals)
        self._index = 0
        self.name = name

    def propose(self, request: PlanRequest) -> PlanProposal:
        if self._index >= len(self._proposals):
            return PlanProposal(
                abstain=True, abstain_reason="scripted sequence exhausted",
                planner_id=self.name, rationale="exhausted",
            )
        proposal = self._proposals[self._index]
        self._index += 1
        return proposal

    @property
    def consumed(self) -> int:
        return self._index

    def capabilities(self) -> PlannerCapabilities:
        return PlannerCapabilities(
            name=self.name,
            tier=PlannerTier.TIER1_RECORDED,
            available=True,
            requires_network=False,
            notes=(f"{len(self._proposals)} recorded proposals",),
        )


__all__ = [
    "GraphEdge",
    "ProfilePlanner",
    "ProbeSet",
    "ReactionRule",
    "ScriptedPlanner",
    "UiGraph",
]
