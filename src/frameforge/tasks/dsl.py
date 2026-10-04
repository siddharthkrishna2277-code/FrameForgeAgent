"""Task / scenario DSL.

A task is an objective plus ordered steps. A scenario is a task plus assertions,
environment metadata and an evidence policy. Both are YAML files under ``tasks/`` and
``profiles/`` - data, reviewable, diffable, and versioned.

Why a DSL rather than Python callbacks: a QA scenario must be readable by the person who
wrote the test (a game developer, not a Python programmer), diffable between builds, and
safe by construction. A Python callback could do anything, including everything the
guardrails forbid. A declared step cannot.

Step semantics that matter:

* ``require`` - preconditions that must hold before the step runs. Failing them does not
  skip the step silently; it routes through the recovery ladder.
* ``verify`` - postconditions. Every step has them; that is the "never assume the action
  worked" rule made structural.
* ``on_unreachable`` - what to do when the step's own anchors never appear: ``fail``,
  ``skip``, or ``unknown``. All three are valid and all three are explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from frameforge.perception.verify import (
    AlwaysTrue,
    AnchorAbsent,
    AnchorVisible,
    ColorProbeMatches,
    Condition,
    NoChange,
    NoInputFor,
    RegionMatches,
    ScreenChanged,
    SignalEquals,
    TextAbsent,
    TextMatches,
    WindowProperty,
)


class OnUnreachable(StrEnum):
    """What to do when a step's expected state never appears."""

    FAIL = "fail"      # a real QA failure: the game never got there
    SKIP = "skip"      # not applicable in this build
    UNKNOWN = "unknown"  # perception insufficient to tell


class StepKind(StrEnum):
    NAVIGATE = "navigate"   # follow a graph/landmarks to a named state
    INTERACT = "interact"   # perform a declared action
    ASSERT = "assert"       # verify only
    SETTLE = "settle"       # wait for stability (loading screens)
    LAUNCH = "launch"       # launch or attach


@dataclass(frozen=True, slots=True)
class StepAction:
    """A declared action in a step, as a discriminated union of simple shapes.

    Deliberately narrower than the full ``Action`` model: a scenario author writes
    ``intent: jump`` or ``click: menu_new_game``, not an ``AnchorObservation``. The
    runner expands these into full actions.
    """

    kind: str
    intent: str = ""
    key: str = ""
    keys: tuple[str, ...] = ()
    at: tuple[int, int] | None = None
    anchor: str = ""
    button: str = "left"
    count: int = 1
    hold_ms: int = 0
    text: str = ""
    scroll_x: int = 0
    scroll_y: int = 0
    dx: int = 0
    dy: int = 0
    steps: int = 12
    duration_ms: int = 120
    #: Whether repeating this action is safe. Typing is *not* idempotent: a retry after a
    #: failed postcondition types the text a second time, which is how a probe string
    #: accumulated three times before this was found against a real application.
    idempotent: bool = True

    def describe(self) -> str:
        if self.kind == "intent":
            return f"intent({self.intent})"
        if self.kind == "key":
            return f"key({self.key})"
        if self.kind == "hotkey":
            return f"hotkey({'+'.join(self.keys)})"
        if self.kind == "click":
            return f"click({self.anchor or self.at})"
        if self.kind == "click_anchor_text":
            return f"click_text({self.anchor})"
        if self.kind == "scroll":
            return f"scroll({self.scroll_x},{self.scroll_y})"
        if self.kind == "mouselook":
            return f"mouselook({self.dx},{self.dy})"
        if self.kind == "type":
            return f"type({len(self.text)} chars)"
        return self.kind


#: Action kinds whose repetition changes state cumulatively. A retry after a failed
#: postcondition must not repeat these: the postcondition usually failed for a perception
#: reason (a misread glyph, a slow repaint), not because the action failed to happen.
NON_IDEMPOTENT_KINDS: frozenset[str] = frozenset({"type", "scroll", "mouselook"})


@dataclass(frozen=True, slots=True)
class TaskStep:
    """One step."""

    name: str
    kind: StepKind = StepKind.INTERACT
    description: str = ""
    #: Anchors that must be visible for this step to be relevant.
    require: tuple[str, ...] = ()
    #: Anchors that must be visible after this step completes.
    expect: tuple[str, ...] = ()
    #: Named condition to verify. ``screen_changed`` by default.
    verify: str = "screen_changed"
    verify_args: dict[str, Any] = field(default_factory=dict)
    #: Action to perform, when the step has one.
    action: StepAction | None = None
    #: The planner action name this step binds, for Tier-0 step-action lookup.
    planner_action: str = ""
    attempts: int = 2
    timeout_ms: int = 30_000
    settle_ms: int = 400
    on_unreachable: OnUnreachable = OnUnreachable.FAIL
    tags: tuple[str, ...] = ()
    #: Seconds to wait for the step to become reachable before giving up.
    reach_timeout_ms: int = 8_000
    #: Whether this step may be repeated automatically. Defaults to the action's own
    #: idempotency: typing and scrolling are never retried blindly, clicking is.
    retryable: bool | None = None

    def is_retryable(self) -> bool:
        if self.retryable is not None:
            return self.retryable
        if self.action is None:
            return False
        return self.action.kind not in NON_IDEMPOTENT_KINDS

    def describe(self) -> str:
        bits = [self.name]
        if self.require:
            bits.append(f"require={list(self.require)}")
        if self.expect:
            bits.append(f"expect={list(self.expect)}")
        if self.action:
            bits.append(self.action.describe())
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    """What to keep. Deliberately conservative by default: a full PNG per decision would
    fill a disk in a long run, and most of those frames are worthless."""

    capture_key_frames: bool = True
    capture_on_failure: bool = True
    capture_on_unknown: bool = True
    periodic_every_n: int = 0        # 0 = never
    video: bool = False
    video_fps: int = 15
    keep_all_actions: bool = False
    ocr_snapshots: bool = True
    #: Redact these regions before storing or transmitting. Applied at the adapter, before
    #: encoding (guardrail G-PER-02).
    redact_regions: tuple[tuple[float, float, float, float], ...] = ()
    #: Include OCR text in the report. Off by default because OCR text is the most likely
    #: place for personal data to appear in a screenshot.
    include_ocr_text_in_report: bool = False
    max_run_mb: int = 200


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """How to bring the target up if it is not already running.

    This is the *direct/test* launch path from docs/ROADMAP.md §8, and it is deliberately
    the fallback rather than the primary: the human/UI path remains
    ``launch_mode: ui``. A scenario that declares a launch spec must also set
    ``allow_direct_launch: true``, and the exact command line is recorded in the report, so
    a reader can always tell a launched run from an attached one.

    Nothing here is a shell string. ``executable`` and ``args`` are separate so nothing is
    ever concatenated into a command line, which is where quoting bugs and injection live.
    """

    executable: str
    args: tuple[str, ...] = ()
    #: Landmark that indicates the app has finished starting. Polled until it appears.
    ready_landmark: str = ""
    #: Seconds to wait for the window and the readiness landmark.
    timeout_ms: int = 20_000
    #: Window to focus before starting, so the app comes up in a known place.
    pre_focus_target: str = ""
    rationale: str = ""

    def argv(self) -> list[str]:
        return [self.executable, *self.args]

    def describe(self) -> str:
        return f"{self.executable} {' '.join(self.args)}".strip()


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    """A task: an objective plus ordered steps."""

    name: str
    objective: str = ""
    description: str = ""
    steps: tuple[TaskStep, ...] = ()
    tags: tuple[str, ...] = ()
    seed: int = 0
    evidence: EvidencePolicy = field(default_factory=EvidencePolicy)
    #: Free-form metadata surfaced in the report (build id, commit, testbed flags...).
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Steps run before ``steps`` to establish a known starting state.
    #:
    #: Added because a scenario that asserts on a document, a menu or a screen needs to
    #: *reset* that thing first. Discovered against real Notepad: a menu left open by an
    #: earlier run made a later run assert against the wrong screen entirely, and nothing in
    #: the system could express "make it look like a fresh start".
    #:
    #: Setup failures are recorded but do not fail the run - a scenario may legitimately
    #: tolerate an already-correct state.
    setup: tuple[TaskStep, ...] = ()
    #: Post-run assertions. A scenario's core purpose.
    assertions: tuple[TaskStep, ...] = ()
    allow_direct_launch: bool = False
    allow_irreversible: bool = False
    #: Optional self-launch. Only honoured when allow_direct_launch is true.
    launch: LaunchSpec | None = None

    def step_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps)

    def find_step(self, name: str) -> TaskStep | None:
        return next((s for s in self.steps if s.name == name), None)


@dataclass(frozen=True, slots=True)
class ScenarioDefinition(TaskDefinition):
    """A scenario is a task plus assertions, environment, and identity."""

    scenario_id: str = ""
    build_id: str = ""
    commit: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    #: Launch mode. ``ui`` is primary and the default; ``direct`` is opt-in and stamped
    #: into the report so nobody mistakes a fast-path run for a human-path run.
    launch_mode: str = "ui"
    direct_launch_command: tuple[str, ...] = ()
    expected_failures: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


def build_condition(spec: str, args: dict[str, Any] | None = None) -> Condition:
    """Turn ``verify: anchor_visible`` plus args into a Condition object.

    A registry rather than a chain of ifs, so adding a condition type is one entry here
    and one in ``__all__``.
    """
    a = dict(args or {})
    registry: dict[str, Any] = {
        "screen_changed": lambda: ScreenChanged(**a),
        "no_change": lambda: NoChange(**a),
        "anchor_visible": lambda: AnchorVisible(anchor=str(a.pop("anchor", "")), **a),
        "anchor_absent": lambda: AnchorAbsent(anchor=str(a.pop("anchor", "")), **a),
        "text_matches": lambda: TextMatches(pattern=str(a.pop("pattern", "")), **a),
        "text_absent": lambda: TextAbsent(pattern=str(a.pop("pattern", "")), **a),
        "color_probe": lambda: ColorProbeMatches(probe=str(a.pop("probe", "")), **a),
        "window_property": lambda: WindowProperty(
            property=str(a.pop("property", "title")), **a
        ),
        "region_matches": lambda: RegionMatches(**a),
        "signal_equals": lambda: SignalEquals(**a),
        "no_input_for": lambda: NoInputFor(**a),
        "always": lambda: AlwaysTrue(),
    }
    factory = registry.get(spec)
    if factory is None:
        msg = f"unknown verify condition {spec!r}; available: {sorted(registry)}"
        raise KeyError(msg)
    return factory()


__all__ = [
    "EvidencePolicy",
    "LaunchSpec",
    "OnUnreachable",
    "ScenarioDefinition",
    "StepAction",
    "StepKind",
    "TaskDefinition",
    "TaskStep",
    "build_condition",
]
