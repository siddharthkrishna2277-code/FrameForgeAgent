"""Deterministic replay: re-run a recorded plan and diff it.

This is the mechanism that makes AI behaviour testable. A live run writes ``plan.jsonl``
with the exact logical action sequence. Replaying feeds that sequence back through the
planner port, and diffing the new plan against the old one answers the question that
matters for a QA tool: *did the same inputs produce the same behaviour?*

A drift is reported as a structured diff rather than a boolean, because "it differed" is
not actionable but "step 3 chose `interact` instead of `confirm`" is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frameforge.actions.model import (
    Action,
    Click,
    Drag,
    GamepadAxis,
    GamepadButton,
    Hotkey,
    Intent,
    KeyDown,
    KeyPress,
    KeyUp,
    MouseButtonDown,
    MouseButtonUp,
    MouseLook,
    MoveMouse,
    Screenshot,
    Scroll,
    TypeText,
    Wait,
)
from frameforge.ports.geometry import Point
from frameforge.ports.input import Key, MouseButton
from frameforge.ports.planner import PlanProposal, PlanStep, PlannerTier
from frameforge.store.runs import RunPaths

#: discriminator -> class. Kept explicit rather than relying on the union's pydantic
#: resolution, so a schema change cannot silently break replay of an old run.
_TYPES: dict[str, type] = {
    "move_mouse": MoveMouse, "click": Click, "mouse_down": MouseButtonDown,
    "mouse_up": MouseButtonUp, "drag": Drag, "scroll": Scroll, "mouse_look": MouseLook,
    "key_press": KeyPress, "key_down": KeyDown, "key_up": KeyUp, "hotkey": Hotkey,
    "type_text": TypeText, "wait": Wait, "screenshot": Screenshot,
    "gamepad_button": GamepadButton, "gamepad_axis": GamepadAxis, "intent": Intent,
}


def _to_point(value: Any) -> Point:
    """Accept a point as ``[x, y]`` or ``{"x": .., "y": ..}``.

    pydantic serialises ``Point`` as an object, but a hand-written or older plan may use a
    two-element list. Replaying should not depend on which serialiser wrote the file.
    """
    if isinstance(value, dict):
        return Point(int(value["x"]), int(value["y"]))
    x, y = value
    return Point(int(x), int(y))


def action_from_dict(data: dict[str, Any]) -> Action:
    """Rebuild a logical action from its recorded form."""
    kind = data.get("type")
    cls = _TYPES.get(str(kind))
    if cls is None:
        msg = f"recorded plan contains unknown action type {kind!r}"
        raise ValueError(msg)
    payload = {k: v for k, v in data.items() if k != "type"}

    if payload.get("at") is not None:
        payload["at"] = _to_point(payload["at"])
    if payload.get("path") is not None:
        payload["path"] = [_to_point(p) for p in payload["path"]]
    for field_name in ("key",):
        if payload.get(field_name):
            payload[field_name] = Key.parse(str(payload[field_name]))
    if payload.get("keys"):
        payload["keys"] = [Key.parse(str(k)) for k in payload["keys"]]
    if payload.get("button"):
        payload["button"] = MouseButton(str(payload["button"]))
    return cls(**payload)


@dataclass(frozen=True, slots=True)
class PlanEntry:
    step: str
    action: Action
    blast: str

    def key(self) -> str:
        return f"{self.step}|{self.action.describe()}"


def load_plan(paths: RunPaths) -> list[PlanEntry]:
    """Read a run's recorded plan. Raises if it is absent."""
    if not paths.plan.exists():
        msg = f"no plan.jsonl in {paths.root} - was the run recorded?"
        raise FileNotFoundError(msg)
    entries: list[PlanEntry] = []
    for line in paths.plan.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        entries.append(PlanEntry(
            step=str(data.get("step", "")),
            action=action_from_dict(data.get("action_record") or {"type": "wait", "ms": 0}),
            blast=str(data.get("blast", "")),
        ))
    return entries


class RecordedPlanner:
    """Replays a recorded action sequence.

    Implements the same port as every other planner, so replay exercises the *real*
    pipeline: validator, compiler, budget, focus guard, executor and verifier. Only the
    decision source is swapped.
    """

    def __init__(self, entries: list[PlanEntry], name: str = "recorded") -> None:
        self._entries = list(entries)
        self._index = 0
        self.name = name

    def propose(self, request) -> PlanProposal:
        """Serve the recorded action for the step being executed.

        A recording is a sequence of ``(step, action)`` pairs, not a flat action list.
        Replaying positionally would hand step 2's action to step 1, which produces a plan
        that looks superficially similar but is semantically different - and a diff that
        reports drift for the wrong reason. Matching on step name makes the comparison mean
        what it says.
        """
        current = getattr(request.context, "current_step", None) if request is not None else None

        if self._index >= len(self._entries):
            return PlanProposal(
                abstain=True, abstain_reason="recorded plan exhausted",
                planner_id=self.name, rationale="exhausted",
            )

        entry = self._entries[self._index]
        if current is not None and entry.step != current:
            # Look ahead for an action recorded for this step, skipping ones the current
            # scenario no longer contains (a step was added or removed since recording).
            match = next((e for e in self._entries[self._index:] if e.step == current), None)
            if match is None:
                return PlanProposal(
                    abstain=True,
                    abstain_reason=f"no recorded action for step {current!r}",
                    planner_id=self.name, rationale="no match",
                )
            entry = match

        self._index += 1
        return PlanProposal(
            steps=(PlanStep(
                action=entry.action,
                expected_change="replayed",
                rationale=f"replay of recorded step {entry.step!r}",
            ),),
            tier=PlannerTier.TIER1_RECORDED,
            planner_id=self.name,
            rationale="replay",
        )

    @property
    def consumed(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._entries)

    def capabilities(self):
        from frameforge.ports.planner import PlannerCapabilities

        return PlannerCapabilities(
            name=self.name, tier=PlannerTier.TIER1_RECORDED, available=True,
            requires_network=False, notes=(f"{self.total} recorded actions",),
        )


@dataclass(slots=True)
class PlanDiff:
    """Structured difference between two recorded plans."""

    identical: bool = True
    original_count: int = 0
    replay_count: int = 0
    differences: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        if self.identical:
            return f"identical: {self.original_count} actions replayed exactly"
        lines = [f"DRIFT: {len(self.differences)} difference(s) "
                 f"({self.original_count} recorded vs {self.replay_count} replayed)"]
        for d in self.differences[:20]:
            lines.append(
                f"  step {d.get('index')}: {d.get('reason')}\n"
                f"    recorded: {d.get('recorded')}\n"
                f"    replayed: {d.get('replayed')}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identical": self.identical,
            "original_count": self.original_count,
            "replay_count": self.replay_count,
            "differences": self.differences,
        }


def diff_plans(original: list[PlanEntry], replayed: list[PlanEntry]) -> PlanDiff:
    """Compare two plans, reporting the first difference per position."""
    diff = PlanDiff(original_count=len(original), replay_count=len(replayed))
    for index in range(max(len(original), len(replayed))):
        before = original[index] if index < len(original) else None
        after = replayed[index] if index < len(replayed) else None
        if before is None:
            diff.identical = False
            diff.differences.append({
                "index": index, "reason": "extra action in replay",
                "recorded": None, "replayed": after.key() if after else None,
            })
            continue
        if after is None:
            diff.identical = False
            diff.differences.append({
                "index": index, "reason": "missing action in replay",
                "recorded": before.key(), "replayed": None,
            })
            continue
        if before.key() != after.key():
            diff.identical = False
            diff.differences.append({
                "index": index, "reason": "action differs",
                "recorded": before.key(), "replayed": after.key(),
            })
    return diff


__all__ = [
    "PlanDiff",
    "PlanEntry",
    "RecordedPlanner",
    "action_from_dict",
    "diff_plans",
    "load_plan",
]
