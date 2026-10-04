"""Input Controller + policy layer: the Codex-style tool boundary.

The architectural lesson worth taking from a mature coding agent is not any particular
function - it is that the model emits a *structured request*, a policy layer decides
whether it is permitted, one constrained executor performs it, and the outcome is logged
and verified. Ordinary coding work never touches the physical keyboard, so it cannot
strand a modifier.

Frame Forge has to drive a real desktop, so it needs the same boundary with a stricter
target contract:

    Planner / Scenario / AI  ->  declarative Action (no OS calls)
      ->  InputPolicy.validate(...)          permission, target, bounds, timeout
        ->  InputController.execute(...)     ONE bounded action
          ->  WindowsInputBackend            the only component emitting OS events
            ->  result + audit record + guaranteed cleanup

Three implementations, so the whole thing is testable without a desktop:

* :class:`MockInputController` - records only. Used by every unit and integration test.
* :class:`WindowsInputController` - the live path. Delegates to
  :class:`~frameforge.actions.safety.InputSafetyManager`.
* :class:`DisabledInputController` - refuses everything; used when input must be off.

The policy layer is what makes "injected input has no target" survivable: an action names
the window it is for, and if that window is not the one currently in front, nothing is
sent and the run pauses.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from frameforge.actions.model import Action
from frameforge.ports.geometry import Point, Rect


class PolicyOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


#: Denial remedies, keyed by enum value. Populated immediately after the enum is defined,
#: because enum members are values rather than plain names and a dict literal cannot
#: reference them before the class exists.
_DENIAL_REMEDIES: dict[str, str] = {}


class DenyReason(StrEnum):
    """Why a request was refused.

    Every variant has a remedy, because a refusal that does not say what to do instead is a
    dead end for whoever hit it. Adopted from Codex's execpolicy, where a ``forbidden`` rule
    is expected to say "use X instead of Y".
    """

    NOT_PERMITTED = "action_not_permitted"
    TARGET_MISSING = "no_target_window"
    TARGET_NOT_ALLOWED = "target_window_not_allowlisted"
    TARGET_NOT_FOREGROUND = "target_window_not_foreground"
    TARGET_MISMATCH = "target_window_identity_changed"
    OUT_OF_BOUNDS = "point_outside_target_bounds"
    RIGHT_CLICK = "right_click_not_permitted"
    DISARMED = "input_disarmed"
    DEADLINE = "action_deadline_exceeded"
    CLEANUP_FAILED = "cleanup_previously_failed"
    IDENTITY_UNVERIFIED = "target_identity_unverified"

    @property
    def remedy(self) -> str:
        """What the operator or planner should do instead."""
        return _DENIAL_REMEDIES.get(
            self.value, "review the scenario and policy configuration"
        )


_DENIAL_REMEDIES.update({
    DenyReason.NOT_PERMITTED.value:
        "add the action type to PolicyConfig.permitted_action_types, or use a different action",
    DenyReason.TARGET_MISSING.value:
        "run with --profile pointing at the target, so the run declares which window it drives",
    DenyReason.TARGET_NOT_ALLOWED.value:
        "add the process to PolicyConfig.allow_processes, or target the correct application",
    DenyReason.TARGET_NOT_FOREGROUND.value:
        "bring the target window to the foreground, or re-run with --refocus so Frame Forge "
        "may raise it",
    DenyReason.TARGET_MISMATCH.value:
        "the target window was replaced; re-resolve it - the runner does this automatically "
        "when resuming from a pause",
    DenyReason.OUT_OF_BOUNDS.value:
        "clamp the point to the target's client rectangle",
    DenyReason.RIGHT_CLICK.value:
        "right-click requires an explicit RightClick action; it is never used as a generic "
        "focus or retry mechanism",
    DenyReason.DISARMED.value:
        "await arming, or await the run's grace period ending",
    DenyReason.DEADLINE.value:
        "increase the action timeout within the scenario's budget",
    DenyReason.CLEANUP_FAILED.value:
        "run 'frameforge recover-input', then re-run; input stays refused until cleanup verifies",
    DenyReason.IDENTITY_UNVERIFIED.value:
        "the target's executable could not be verified by image path; ensure the process is "
        "not running elevated relative to Frame Forge",
})


@dataclass(frozen=True, slots=True)
class ActionTarget:
    """The only window an action may touch.

    ``image_path`` carries the *identity* of the target executable, as distinct from
    ``process_name`` which is only a label.

    Named distinctly from ``ports.window.TargetSpec`` (which describes how to *find* a
    window). Two types with the same name and different meaning is exactly the ambiguity
    that produced a runtime ``TypeError`` when both were imported in one module.
    """

    hwnd: int
    pid: int = 0
    process_name: str = ""
    class_name: str = ""
    title: str = ""
    bounds: Rect | None = None
    image_path: str = ""

    def describe(self) -> str:
        return (f"{self.process_name or '?'}[{self.pid}] "
                f"{self.class_name or '?'} {self.title[:36]!r} hwnd={self.hwnd}")

    def identity_ok(self, expected_image_path: str = "") -> tuple[bool, str]:
        """Is this really the program it claims to be?

        A process name is self-asserted. Three checks, cheapest first:

        1. the image path resolves at all - an unresolved identity is not a verified one;
        2. the image path exists on disk;
        3. the basename agrees with the reported process name;
        4. if the profile declares an expected path, it matches exactly.

        Adopted from Codex's execpolicy, where basename fallback from ``/usr/bin/git`` to a
        rule written for ``git`` is a documented and gated behaviour - the lesson being
        that program identity needs an absolute-path component, not a mutable string.
        """
        import os as _os

        if not self.pid:
            return False, "target has no pid"
        image = self.image_path
        if not image:
            return False, "target image path could not be resolved"
        if not _os.path.exists(image):
            return False, f"target image path does not exist: {image}"
        base = _os.path.basename(image).lower()
        named = (self.process_name or "").lower()
        if named and base != named:
            return False, f"image basename {base!r} disagrees with process name {named!r}"
        if expected_image_path:
            if image.lower() != expected_image_path.lower():
                return False, (
                    f"image path {image!r} does not match the profile's expected "
                    f"{expected_image_path!r}"
                )
        return True, ""


@dataclass(frozen=True, slots=True)
class PolicyResult:
    outcome: PolicyOutcome
    reason: DenyReason | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOWED


@dataclass(slots=True)
class ToolRequest:
    """A declarative request. Contains no OS calls and no coordinates we invented."""

    action: Action
    run_id: str = ""
    action_id: str = ""
    source: str = ""
    target: ActionTarget | None = None
    timeout_ms: int = 5000
    max_hold_ms: float = 2000.0
    #: Explicit opt-in for the right mouse button, which is otherwise refused.
    allow_right_click: bool = False
    reason: str = ""


@dataclass(slots=True)
class ToolResult:
    """Structured outcome, so the planner learns what happened without parsing prose."""

    ok: bool
    action_id: str
    outcome: str = "executed"
    detail: str = ""
    reason: DenyReason | None = None
    duration_ms: float = 0.0
    held_after: tuple[str, ...] = ()
    cleanup_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "action_id": self.action_id, "outcome": self.outcome,
            "detail": self.detail,
            "reason": self.reason.value if self.reason else None,
            "duration_ms": round(self.duration_ms, 2),
            "held_after": list(self.held_after),
            "cleanup_required": self.cleanup_required,
        }


@dataclass(slots=True)
class PolicyConfig:
    """What a scenario is permitted to do."""

    #: Process names an action may target. Empty means "no live input at all".
    allow_processes: frozenset[str] = frozenset()
    #: Absolute image path the target must have, when the profile declares one.
    expected_image_path: str = ""
    #: Require the target window to be foreground before sending. On by default.
    require_foreground: bool = True
    #: Require the point of a click to be inside the target's bounds.
    enforce_bounds: bool = True
    allow_right_click: bool = False
    #: Action kinds this controller will execute at all.
    permitted_action_types: frozenset[str] = frozenset({
        "move_mouse", "click", "mouse_down", "mouse_up", "drag", "scroll",
        "mouse_look", "key_press", "key_down", "key_up", "hotkey", "type_text",
        "wait", "intent",
    })
    max_hold_ms: float = 2000.0
    default_timeout_ms: int = 5000


class InputPolicy:
    """Decides whether a request is permitted. Never emits input."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()
        self.denials: list[tuple[str, str]] = []

    def configure_for_target(self, target: ActionTarget) -> None:
        """Allow exactly this window and program, for the lifetime of the run."""
        if target.process_name:
            self.config.allow_processes = frozenset({target.process_name.lower()})
        else:
            self.config.allow_processes = frozenset()
        self.config.expected_image_path = target.image_path

    def validate(self, request: ToolRequest, *, is_armed: bool = True,
                 cleanup_ok: bool = True) -> PolicyResult:
        """Every precondition, checked before anything is sent."""
        from frameforge.actions.safety import classify_target, read_foreground_window

        cfg = self.config
        action = request.action

        # Default-deny. With no allow-listed process, live input is refused outright -
        # whether or not a target was attached.
        #
        # The earlier version only fired when a target was present, so a request carrying
        # *no* target at all passed the check. That is exactly the dangerous case: an action
        # with no declared destination is the definition of "wherever the cursor happens to
        # be", which is the behaviour incident 2 was about.
        if not cfg.allow_processes:
            if request.target is None:
                return self._deny(request, DenyReason.TARGET_MISSING,
                                  "live input requires an explicitly named target window; "
                                  "no target process is allow-listed")
            return self._deny(request, DenyReason.TARGET_NOT_ALLOWED,
                              f"process {(request.target.process_name or '?').lower()!r} is "
                              f"not in the allow-list {sorted(cfg.allow_processes)}")

        if not cleanup_ok:
            return self._deny(request, DenyReason.CLEANUP_FAILED,
                              "a previous cleanup failed; refusing further input")
        if action.type not in cfg.permitted_action_types:
            return self._deny(request, DenyReason.NOT_PERMITTED,
                              f"action type {action.type!r} is not permitted")
        if not is_armed:
            return self._deny(request, DenyReason.DISARMED, "input is disarmed")

        if cfg.allow_processes:
            if request.target is None:
                return self._deny(request, DenyReason.TARGET_MISSING,
                                  "live input requires an explicit target window")
            proc = (request.target.process_name or "").lower()
            if not proc:
                return self._deny(request, DenyReason.TARGET_NOT_ALLOWED,
                                  "target window has no resolved process name")
            if proc not in cfg.allow_processes:
                return self._deny(request, DenyReason.TARGET_NOT_ALLOWED,
                                  f"process {proc!r} is not in the allow-list "
                                  f"{sorted(cfg.allow_processes)}")
            # Identity, not just a self-asserted name.
            ok_identity, why = request.target.identity_ok(cfg.expected_image_path)
            if not ok_identity:
                return self._deny(request, DenyReason.IDENTITY_UNVERIFIED, why)

        if request.target is not None and cfg.require_foreground:
            fg = read_foreground_window()
            if fg is None:
                return self._deny(request, DenyReason.TARGET_NOT_FOREGROUND,
                                  "no foreground window")
            ok, why = classify_target(fg)
            if not ok:
                return self._deny(request, DenyReason.TARGET_NOT_ALLOWED, why)
            if fg.hwnd != request.target.hwnd:
                return self._deny(
                    request, DenyReason.TARGET_NOT_FOREGROUND,
                    f"foreground {fg.describe()} is not the target "
                    f"{request.target.describe()}",
                )

        if request.target is not None and cfg.enforce_bounds:
            point = _point_of(action)
            if point is not None and request.target.bounds is not None:
                if not request.target.bounds.contains(point):
                    return self._deny(
                        request, DenyReason.OUT_OF_BOUNDS,
                        f"point {point.as_tuple()} is outside the target bounds "
                        f"{request.target.bounds.as_tuple()}",
                    )

        if (getattr(action, "button", "") == "right"
                and not (cfg.allow_right_click or request.allow_right_click)):
            return self._deny(request, DenyReason.RIGHT_CLICK,
                              "right-click requires explicit permission")

        return PolicyResult(outcome=PolicyOutcome.ALLOWED)

    def _deny(self, request: ToolRequest, reason: DenyReason, detail: str) -> PolicyResult:
        self.denials.append((request.action_id, reason.value))
        combined = f"{detail}. To proceed: {reason.remedy}"
        return PolicyResult(outcome=PolicyOutcome.DENIED, reason=reason, detail=combined)


def _point_of(action: Action) -> Point | None:
    at = getattr(action, "at", None)
    return at if isinstance(at, Point) else None


class InputController(ABC):
    """Executes one bounded action. The only component permitted to emit OS input."""

    name = "abstract"

    @abstractmethod
    def execute(self, request: ToolRequest, compiled_primitives: list[Any]) -> ToolResult:
        """Perform the action. Must guarantee cleanup on every path."""

    @abstractmethod
    def cleanup(self) -> dict[str, Any]:
        """Release everything and stop workers. Idempotent."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Non-invasive check that nothing is held and input is quiescent."""

    @property
    def held(self) -> tuple[str, ...]:
        return ()


class MockInputController(InputController):
    """Records actions without touching the desktop.

    Every unit and integration test runs against this. It is the reason the test suite can
    cover cancellation, worker crashes and hold deadlines exhaustively - none of which can
    be provoked safely against a real desktop.
    """

    name = "mock"

    def __init__(self, *, armed: bool = True, fail_on: str | None = None,
                 hold_mid_action: bool = False) -> None:
        #: Every primitive this controller was asked to dispatch, in order.
        #:
        #: The authoritative record for tests and audits. It sits *before* the port on
        #: purpose: a test asserting on the port would miss the case where the gate
        #: correctly refused and the controller never called it.
        self.calls: list[Any] = []
        self.armed = armed
        #: Test hook: raise from execute for this action type.
        self.fail_on = fail_on
        #: Test hook: leave a hold behind, as a crash between down and up would.
        self.hold_mid_action = hold_mid_action
        self.executed: list[ToolResult] = []
        self.primitives_seen: list[Any] = []
        self.cleanup_calls = 0
        self._held: set[str] = set()

    def execute(self, request: ToolRequest, compiled_primitives: list[Any]) -> ToolResult:
        t0 = time.perf_counter()
        self.calls.extend(compiled_primitives)
        self.primitives_seen.extend(compiled_primitives)
        if self.fail_on and request.action.type == self.fail_on:
            self._held.add("simulated_hold")
            raise RuntimeError(f"mock backend failure on {request.action.type}")
        if self.hold_mid_action:
            # Leaves a hold behind, exactly as a real crash between down and up would.
            self._held.add("simulated_hold")
        result = ToolResult(
            ok=True, action_id=request.action_id, outcome="executed",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            held_after=tuple(sorted(self._held)),
            cleanup_required=bool(self._held),
        )
        # Recorded so a caller - or a test - can inspect what the controller did.
        self.executed.append(result)
        return result

    def cleanup(self) -> dict[str, Any]:
        self.cleanup_calls += 1
        released = sorted(self._held)
        self._held.clear()
        return {"released": released, "errors": [], "keys": [], "buttons": []}

    def health(self) -> dict[str, Any]:
        return {"healthy": not self._held, "held": sorted(self._held), "mock": True}

    @property
    def held(self) -> tuple[str, ...]:
        return tuple(sorted(self._held))


class DisabledInputController(InputController):
    """Refuses everything. Used when a run must not touch the machine at all."""

    name = "disabled"

    def execute(self, request: ToolRequest, compiled_primitives: list[Any]) -> ToolResult:
        return ToolResult(ok=False, action_id=request.action_id, outcome="refused",
                          detail="input is disabled by configuration")

    def cleanup(self) -> dict[str, Any]:
        return {"released": [], "errors": [], "note": "input was disabled"}

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "disabled": True}


class WindowsInputController(InputController):
    """Live controller. The single owner of real Windows input for the whole project.

    Everything that could strand a key lives behind :class:`InputSafetyManager`: hold
    deadlines, the defensive release sweep, chord refusal, the context-menu key guard,
    right-click gating, target validation and the event-level audit.
    """

    name = "windows"

    def __init__(self, port, *, policy: InputPolicy | None = None,
                 verbose_events: bool = False) -> None:
        safety = getattr(port, "safety", None)
        if safety is None:
            msg = (
                "WindowsInputController requires a port with a safety manager "
                "(InputSafetyManager). A port without one cannot guarantee cleanup, so "
                "live input is refused rather than unguarded."
            )
            raise ValueError(msg)
        if policy is None:
            # Required, and deliberately so. It was optional with a permissive default,
            # which meant a live controller could exist with a policy that allow-lists
            # nothing - and the *executor* was the only thing consulting it. Authority to
            # inject and authority to permit must not be held by different components, so
            # the controller owns the check and cannot be built without one.
            msg = (
                "WindowsInputController requires an InputPolicy. The controller performs "
                "the authoritative check at the point of injection, so there is no path to "
                "the OS that does not pass validation."
            )
            raise ValueError(msg)
        self._port = port
        self._safety = safety
        self._safety.verbose_events = verbose_events
        self.policy = policy
        self._policy = policy
        self.history: list[ToolResult] = []

    def execute(self, request: ToolRequest, compiled_primitives: list[Any]) -> ToolResult:
        """Execute one bounded action.

        The policy check lives here, at the point of injection, and is *not* optional. A
        caller may pre-validate to produce a better error, but a request that reaches this
        method is validated again before anything is sent - so the authority to inject and
        the authority to permit cannot be held by different components.
        """
        t0 = time.perf_counter()
        armed = bool(getattr(self._port, "enabled", False))
        verdict = self._policy.validate(
            request, is_armed=armed, cleanup_ok=self._safety.cleanup_ok
        )
        if not verdict.allowed:
            self.history.append(ToolResult(
                ok=False, action_id=request.action_id, outcome="denied",
                detail=verdict.detail,
                reason=verdict.reason,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            ))
            return self.history[-1]

        self._safety.max_hold_ms = min(self._safety.max_hold_ms, request.max_hold_ms)
        self._safety.target_hwnd = request.target.hwnd if request.target else None
        try:
            sent = self._port.send_batch(compiled_primitives, action_id=request.action_id)
        except Exception as exc:
            self.cleanup()
            return ToolResult(
                ok=False, action_id=request.action_id, outcome="failed",
                detail=f"{type(exc).__name__}: {exc}", cleanup_required=True,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        held = self._safety.held
        result = ToolResult(
            ok=True, action_id=request.action_id,
            outcome="executed" if sent else "blocked",
            detail=f"{sent} primitive(s)",
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            held_after=held,
            cleanup_required=bool(held),
        )
        self.history.append(result)
        return result

    def cleanup(self) -> dict[str, Any]:
        return self._port.release_all()

    def health(self) -> dict[str, Any]:
        return self._safety.post_run_check()

    @property
    def held(self) -> tuple[str, ...]:
        return self._safety.held


def build_controller(port, *, policy: InputPolicy | None = None,
                    verbose_events: bool = False,
                    prefer_mock: bool = False) -> InputController:
    """Choose the controller for a port.

    Kept as one function so the choice is made in exactly one place. ``prefer_mock`` is for
    ``frameforge simulate`` and for tests; a live run must never select the mock by
    accident, so the default is chosen from the port itself and an unrecognised port is
    treated as live rather than being silently given a recorder.
    """
    if prefer_mock:
        return MockInputController()
    name = (getattr(port, "name", "") or "").lower()
    if name in ("fake", "fake_input", "mock", "dry_run"):
        return MockInputController()
    if name in ("disabled", "none"):
        return DisabledInputController()
    if policy is None and not prefer_mock:
        msg = (
            "a live controller requires an InputPolicy; refusing to build one that can "
            "inject without validation"
        )
        raise ValueError(msg)
    # Anything else is treated as live and wrapped. Defaulting the other way would let a
    # new adapter bypass the policy layer simply by not being recognised.
    return WindowsInputController(port, policy=policy, verbose_events=verbose_events)


__all__ = [
    "DenyReason",
    "build_controller",
    "DisabledInputController",
    "InputController",
    "InputPolicy",
    "MockInputController",
    "PolicyConfig",
    "PolicyOutcome",
    "PolicyResult",
    "ActionTarget",
    "ToolRequest",
    "ToolResult",
    "WindowsInputController",
]
