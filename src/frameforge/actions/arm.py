"""User-armed execution state machine, input profiles, gameplay verification, and capture
freshness.

The invariant this module exists to enforce:

    NO USER-ARMED, VERIFIED IN-GAME STATE = NO INPUT.

The state machine is the *single* authority on whether live input may exist. Nothing else
may enable it - not a planner, not a scenario, not an AI, not a test. Every live action must
pass :meth:`ExecutionStateMachine.authorize`, and the answer is a function of the state
plus eleven independent runtime conditions. Any one of them being false means zero input,
a cancelled queue, a defensive release, and a visible reason.

Scope, deliberately narrow: this does **not** launch games, automate startup or login,
navigate menus, move windows, or recover focus. The user launches the game, reaches
gameplay, and manages focus. Frame Forge observes and, only when armed, injects an
allowlisted profile.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable


class ExecutionState(StrEnum):
    """The only states. ``ACTIVE`` is the only one that permits input."""

    OFF = "off"                        # default at startup; nothing is permitted
    OBSERVE = "observe"                # capture/preview only
    TARGET_SELECTED = "target_selected"  # a session exists; still no input
    READY = "ready"                     # session valid, capture fresh, not armed
    ARMED_COUNTDOWN = "armed_countdown"  # user armed; input still disabled
    ACTIVE = "active"                   # the ONLY state permitting input
    PAUSED_SAFE_STOP = "paused_safe_stop"
    EMERGENCY_STOP = "emergency_stop"
    COMPLETED = "completed"


#: States in which live input is permitted. Deliberately a one-element set: adding to it is
#: the single most dangerous edit in this codebase.
INPUT_PERMITTING_STATES: frozenset[ExecutionState] = frozenset({ExecutionState.ACTIVE})


class BlockReason(StrEnum):
    """Why input was refused. Every value is reportable to the user verbatim."""

    NO_ARM_TOKEN = "no_user_arm_token"
    NOT_ARMED = "not_in_active_state"
    SESSION_MISSING = "target_session_missing"
    SESSION_EXPIRED = "target_session_expired"
    TARGET_HWND_INVALID = "target_hwnd_invalid"
    TARGET_PROCESS_MISMATCH = "target_process_mismatch"
    TARGET_NOT_FOREGROUND = "target_not_foreground"
    TARGET_NOT_REGISTERED = "target_not_registered"
    SELF_FOREGROUND = "agent_window_is_foreground"
    PROTECTED_AT_POINT = "protected_window_at_point"
    POINT_NOT_IN_TARGET = "point_not_in_target"
    POINT_OUTSIDE_REGION = "point_outside_allowed_region"
    CAPTURE_STALE = "capture_stale"
    CAPTURE_UNAVAILABLE = "capture_unavailable"
    GAMEPLAY_UNCONFIRMED = "gameplay_not_user_confirmed"
    GAMEPLAY_STATE_BLOCKED = "gameplay_state_blocks_input"
    ESTOP_ACTIVE = "emergency_stop_active"
    PROFILE_FORBIDS_ACTION = "profile_forbids_this_action"
    TOPOLOGY_CHANGED = "monitor_topology_changed"
    DPI_CHANGED = "dpi_changed"
    COUNTDOWN_ACTIVE = "countdown_in_progress"
    DEADLINE_EXCEEDED = "action_deadline_exceeded"
    SYSTEM_DIALOG_AT_POINT = "unexpected_overlay_at_point"


@dataclass(frozen=True, slots=True)
class BlockResult:
    """A structured refusal. Never a silent drop."""

    blocked: bool
    reason: BlockReason | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """Readable inverse of ``blocked``.

        Callers write ``result.allowed``, which is easy to confuse with the ``allow()``
        constructor; having both, spelled the same way, is a trap.
        """
        return not self.blocked

    @classmethod
    def allow(cls) -> BlockResult:
        return cls(blocked=False)

    @classmethod
    def deny(cls, reason: BlockReason, detail: str = "") -> BlockResult:
        return cls(blocked=True, reason=reason, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": self.blocked,
                "reason": self.reason.value if self.reason else None,
                "detail": self.detail}


# --------------------------------------------------------------- gameplay state


class GameplayState(StrEnum):
    """What the target window appears to be showing.

    These are genuinely different facts. A running process is not a visible game; a visible
    window is not a controllable character.
    """

    ACTIVE_GAMEPLAY = "active_gameplay"
    LIKELY_GAMEPLAY = "likely_gameplay"
    MENU = "menu"
    PAUSED = "paused"
    LOADING = "loading"
    CUTSCENE = "cutscene"
    UNKNOWN = "unknown"
    CAPTURE_UNAVAILABLE = "capture_unavailable"


#: Only ACTIVE_GAMEPLAY may permit input. LIKELY_GAMEPLAY is deliberately *not* enough:
#: uncertainty about whether the character is controllable is a reason to stop, not a reason
#: to proceed on a hunch.
INPUT_PERMITTING_GAMEPLAY: frozenset[GameplayState] = frozenset({GameplayState.ACTIVE_GAMEPLAY})


@dataclass(slots=True)
class GameplayVerdict:
    state: GameplayState = GameplayState.UNKNOWN
    confidence: float = 0.0
    reasoning: str = ""
    source: str = "none"

    @property
    def permits_input(self) -> bool:
        return self.state in INPUT_PERMITTING_GAMEPLAY and self.confidence >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "confidence": round(self.confidence, 3),
                "permits_input": self.permits_input, "reasoning": self.reasoning,
                "source": self.source}


# ------------------------------------------------------------------ arm token


@dataclass(slots=True)
class ArmToken:
    """An explicit, expiring, session-bound authorisation from the user.

    Deliberately not reusable. It is bound to one target session, expires quickly, and is
    voided by target change, focus loss, capture loss, pause, stop, topology or DPI change,
    or expiry - so a stale authorisation can never authorise a later run.
    """

    token_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    profile_name: str = ""
    #: The user's explicit words. Bound to this token; never inferred.
    user_attestation: str = ""
    created_mono_ms: float = field(default_factory=lambda: time.monotonic() * 1000.0)
    expires_mono_ms: float = 120_000.0
    voided_reason: str = ""
    consumed: bool = False

    @property
    def age_ms(self) -> float:
        return time.monotonic() * 1000.0 - self.created_mono_ms

    @property
    def valid(self) -> bool:
        return not self.voided_reason and not self.consumed and self.age_ms < self.expires_mono_ms

    def void(self, reason: str) -> None:
        self.voided_reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id, "session_id": self.session_id,
            "profile": self.profile_name, "attestation": self.user_attestation[:120],
            "age_ms": round(self.age_ms), "expires_ms": self.expires_mono_ms,
            "valid": self.valid, "voided_reason": self.voided_reason,
            "consumed": self.consumed,
        }


# --------------------------------------------------------------- input profile


@dataclass(frozen=True, slots=True)
class InputProfile:
    """A named, explicit declaration of what may be injected.

    The planner may not invent input: it can only choose from this list, and this list is
    written by the operator.
    """

    name: str
    description: str = ""
    #: Logical intents and their maximum hold. Keys are intent names, not raw scancodes,
    #: so a profile stays meaningful if a binding changes.
    allowed_keys: frozenset[str] = frozenset()
    max_hold_ms: dict[str, int] = field(default_factory=dict)
    allow_left_click: bool = False
    allow_right_click: bool = False
    allow_scroll: bool = False
    allow_text_entry: bool = False
    max_mouse_delta_px: int = 0
    max_mouse_rate_hz: float = 20.0
    max_actions: int = 200
    duration_limit_ms: float = 60_000.0
    #: Always empty in the default profile, and enforced independently of any profile field.
    prohibited_intents: frozenset[str] = frozenset()
    #: Intents that can never be injectable, whatever this profile says.
    #:
    #: Defaults to ``SYSTEM_INTENTS`` and is *unioned* with it in :meth:`permits` rather
    #: than merely defaulted. A profile that lists ``alt`` in ``allowed_keys`` and leaves
    #: this empty previously permitted Alt - and Alt is how layout switches, Alt+Tab and
    #: Alt+F4 happen. A guard that a caller can switch off by omitting a field is not a
    #: guard.
    system_intents: frozenset[str] = field(default_factory=lambda: SYSTEM_INTENTS)

    def permits(self, intent: str) -> tuple[bool, str]:
        # Union, not intersection: SYSTEM_INTENTS cannot be opted out of.
        blocked = SYSTEM_INTENTS | set(self.system_intents)
        if intent in blocked:
            return False, f"'{intent}' is a system-level intent and is never injectable"
        if intent in self.prohibited_intents:
            return False, f"'{intent}' is prohibited by profile {self.name!r}"
        if intent not in self.allowed_keys:
            return False, f"'{intent}' is not in the allow-list of profile {self.name!r}"
        return True, ""

    def hold_limit_ms(self, intent: str) -> int:
        return int(self.max_hold_ms.get(intent, 500))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "allowed_keys": sorted(self.allowed_keys),
            "max_hold_ms": dict(self.max_hold_ms),
            "allow_left_click": self.allow_left_click,
            "allow_right_click": self.allow_right_click,
            "allow_scroll": self.allow_scroll,
            "allow_text_entry": self.allow_text_entry,
            "max_mouse_delta_px": self.max_mouse_delta_px,
            "max_mouse_rate_hz": self.max_mouse_rate_hz,
            "max_actions": self.max_actions,
            "duration_limit_ms": self.duration_limit_ms,
            "prohibited_intents": sorted(self.prohibited_intents),
        }


#: Intents that can reach Windows itself rather than a game. Never injectable, whatever a
#: profile says, because each of these is a language, layout, or window-switch action.
SYSTEM_INTENTS: frozenset[str] = frozenset({
    "alt", "lalt", "ralt",            # Alt+Shift / Alt+Tab / Alt+F4
    "ctrl", "lctrl", "rctrl",          # Ctrl+Tab, Ctrl+Alt+Del
    "shift", "lshift", "rshift",       # Shift+Switch, Shift+F10
    "win", "lwin", "rwin", "winleft", # Win+Space, Win+Tab
    "menu", "apps", "contextmenu",     # the context-menu key
    "winleft", "tab",                 # window cycling
    "print", "save", "close", "quit", "shutdown", "restart",
})

#: The first supported profile: movement only.
DEFAULT_EARLY_GAMEPLAY_PROFILE = InputProfile(
    name="early_gameplay_movement",
    description=(
        "Short bounded movement only. No clicking, no modifiers, no system keys, no text "
        "entry. This is the conservative first profile and it is what should be used until "
        "a game-specific profile has been validated for a specific title."
    ),
    allowed_keys=frozenset({"forward", "back", "strafe_left", "strafe_right"}),
    max_hold_ms={"forward": 400, "back": 400, "strafe_left": 300, "strafe_right": 300},
    allow_left_click=False,
    allow_right_click=False,
    allow_scroll=False,
    allow_text_entry=False,
    max_actions=40,
    duration_limit_ms=30_000.0,
    system_intents=SYSTEM_INTENTS,
)


# ------------------------------------------------------------ capture freshness


@dataclass(slots=True)
class CaptureFrame:
    """One capture, bound to the target session that produced it."""

    frame_id: str
    run_id: str
    session_id: str
    target_hwnd: int
    target_pid: int
    captured_mono_ms: float
    #: Lifetime after which the frame is stale. Perception is only meaningful if it
    #: describes *now*; a stale frame can authorise an action against a screen that has moved on.
    max_age_ms: float = 750.0
    width: int = 0
    height: int = 0
    client_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    window_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    monitor_index: int = 0
    monitor_device: str = ""
    dpi: int = 96
    topology_fingerprint: str = ""
    source: str = "windows"
    transform_version: str = "1"
    healthy: bool = True
    health_detail: str = ""

    @property
    def age_ms(self) -> float:
        return time.monotonic() * 1000.0 - self.captured_mono_ms

    @property
    def stale(self) -> bool:
        return self.age_ms > self.max_age_ms

    def usable(self) -> tuple[bool, str]:
        if not self.healthy:
            return False, self.health_detail or "capture is unhealthy"
        if self.stale:
            return False, f"capture is {self.age_ms:.0f}ms old (max {self.max_age_ms:.0f}ms)"
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id, "run_id": self.run_id,
            "session_id": self.session_id, "target_hwnd": self.target_hwnd,
            "target_pid": self.target_pid, "age_ms": round(self.age_ms),
            "max_age_ms": self.max_age_ms, "width": self.width, "height": self.height,
            "client_rect": list(self.client_rect), "window_rect": list(self.window_rect),
            "monitor_index": self.monitor_index, "monitor_device": self.monitor_device,
            "dpi": self.dpi, "topology_fingerprint": self.topology_fingerprint,
            "source": self.source, "transform_version": self.transform_version,
            "healthy": self.healthy, "health_detail": self.health_detail,
        }


# ------------------------------------------------------------- the state machine


@dataclass(slots=True)
class ExecutionDecision:
    """Why a live action may or may not proceed. Every field is reportable."""

    state: ExecutionState
    permitted: bool
    reason: BlockReason | None = None
    detail: str = ""
    checks: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "permitted": self.permitted,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "checks": self.checks,
        }


class ExecutionStateMachine:
    """The single authority on whether live input may exist.

    Transitions are explicit and every one is recorded. There is deliberately no
    ``activate()``, no ``force()``, and no module-level toggle: the only way to ACTIVE is
    :meth:`begin_countdown` followed by :meth:`confirm_active`, and both require an arm
    token that the *user* created.
    """

    #: Legal transitions. Anything not listed is refused, not coerced.
    TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
        ExecutionState.OFF: frozenset({ExecutionState.OBSERVE}),
        ExecutionState.OBSERVE: frozenset({
            ExecutionState.TARGET_SELECTED, ExecutionState.OFF}),
        ExecutionState.TARGET_SELECTED: frozenset({
            ExecutionState.READY, ExecutionState.OBSERVE, ExecutionState.OFF}),
        ExecutionState.READY: frozenset({
            ExecutionState.ARMED_COUNTDOWN, ExecutionState.TARGET_SELECTED,
            ExecutionState.OBSERVE, ExecutionState.OFF}),
        ExecutionState.ARMED_COUNTDOWN: frozenset({
            ExecutionState.ACTIVE, ExecutionState.PAUSED_SAFE_STOP,
            ExecutionState.READY, ExecutionState.OFF}),
        ExecutionState.ACTIVE: frozenset({
            ExecutionState.PAUSED_SAFE_STOP, ExecutionState.EMERGENCY_STOP,
            ExecutionState.COMPLETED}),
        ExecutionState.PAUSED_SAFE_STOP: frozenset({
            ExecutionState.READY, ExecutionState.OBSERVE, ExecutionState.OFF,
            ExecutionState.EMERGENCY_STOP}),
        ExecutionState.EMERGENCY_STOP: frozenset({
            ExecutionState.OBSERVE,   # only a completely fresh selection flow
            ExecutionState.OFF,
        }),
        ExecutionState.COMPLETED: frozenset({
            ExecutionState.OBSERVE,   # a new explicit arm flow is required
            ExecutionState.OFF,
        }),
    }

    def __init__(self, run_id: str = "", *, release_hook: Callable[[], Any] | None = None) -> None:
        self.run_id = run_id
        self.state = ExecutionState.OFF
        #: Called whenever input must stop, so held keys/buttons are released.
        self._release_hook = release_hook
        self.arm_token: ArmToken | None = None
        self.profile: InputProfile | None = None
        self.session: Any = None
        self.last_capture: CaptureFrame | None = None
        self.gameplay: GameplayVerdict = GameplayVerdict()
        self.countdown_started_ms: float = 0.0
        self.countdown_ms: float = 4000.0
        self.history: list[dict[str, Any]] = []
        #: The agent's own pid, so a self-target can never be authorised.
        import os as _os

        self.self_pid = _os.getpid()
        self.self_hwnd: int | None = None
        self.actions_used = 0
        self.activated_mono_ms: float = 0.0

    # ------------------------------------------------------------- transitions

    def _to(self, nxt: ExecutionState, reason: str = "") -> bool:
        if nxt not in self.TRANSITIONS.get(self.state, frozenset()):
            return False
        previous = self.state
        self.state = nxt
        self.history.append({
            "from": previous.value, "to": nxt.value, "reason": reason,
            "mono_ms": round(time.monotonic() * 1000.0, 2),
        })
        # Leaving a state that permits input must release everything, immediately.
        if previous in INPUT_PERMITTING_STATES and nxt not in INPUT_PERMITTING_STATES:
            self._release()
            self.void_arm(f"state left {previous.value}")
        return True

    def _release(self) -> None:
        if self._release_hook is not None:
            try:
                self._release_hook()
            except Exception:
                pass

    def void_arm(self, reason: str) -> None:
        if self.arm_token is not None and not self.arm_token.voided_reason:
            self.arm_token.void(reason)

    # ------------------------------------------------------------ user actions

    def observe(self) -> bool:
        """Enter OBSERVE. The only transition available from OFF."""
        return self._to(ExecutionState.OBSERVE, "user requested observation")

    def select_target(self, session: Any, profile: InputProfile) -> bool:
        """Register a target the user selected. No input; never will be from here."""
        self.session = session
        self.profile = profile
        return self._to(ExecutionState.TARGET_SELECTED,
                        f"registered hwnd={getattr(session, 'hwnd', '?')}")

    def mark_ready(self, capture: CaptureFrame) -> bool:
        self.last_capture = capture
        return self._to(ExecutionState.READY, "target session valid and capture fresh")

    def begin_countdown(self, token: ArmToken, *, countdown_ms: float = 4000.0) -> BlockResult:
        """The user armed a selected profile. Input stays disabled through the countdown."""
        if self.state is not ExecutionState.READY:
            return BlockResult.deny(BlockReason.NOT_ARMED,
                                   f"state is {self.state.value}, expected ready")
        if self.profile is None or self.session is None:
            return BlockResult.deny(BlockReason.SESSION_MISSING, "no registered target or profile")
        capture = self.last_capture
        if capture is None:
            return BlockResult.deny(BlockReason.CAPTURE_UNAVAILABLE, "no capture")
        ok, why = capture.usable()
        if not ok:
            return BlockResult.deny(BlockReason.CAPTURE_STALE, why)
        if not token.valid:
            return BlockResult.deny(BlockReason.NO_ARM_TOKEN,
                                   token.voided_reason or "arm token is invalid")
        session_id = getattr(self.session, "session_id", None) or str(id(self.session))
        if token.session_id and token.session_id != session_id:
            return BlockResult.deny(BlockReason.TARGET_NOT_REGISTERED,
                                   "arm token is bound to a different target session")

        self.arm_token = token
        self.countdown_ms = countdown_ms
        self.countdown_started_ms = time.monotonic() * 1000.0
        self._to(ExecutionState.ARMED_COUNTDOWN,
                 f"armed profile={token.profile_name!r}")
        return BlockResult.allow()

    def confirm_active(self, gameplay: GameplayVerdict | None = None) -> BlockResult:
        """Countdown expired. Re-validate everything; enter ACTIVE only if all pass.

        This is the last gate before input exists, so it re-checks the target, the
        foreground, the capture and the gameplay state rather than trusting the countdown.
        """
        if self.state is not ExecutionState.ARMED_COUNTDOWN:
            return BlockResult.deny(BlockReason.NOT_ARMED,
                                   f"state is {self.state.value}, expected countdown")
        elapsed = time.monotonic() * 1000.0 - self.countdown_started_ms
        if elapsed < self.countdown_ms:
            return BlockResult.deny(BlockReason.COUNTDOWN_ACTIVE,
                                   f"{self.countdown_ms - elapsed:.0f}ms remaining")

        # Record the verdict *before* evaluating: the runtime conditions check gameplay
        # state, so evaluating first tested the default UNKNOWN and ACTIVE could never be
        # reached. A gate that can never open is indistinguishable from a gate that is
        # always closed, which is why this had to be an ordering fix and not a default.
        if gameplay is not None:
            self.gameplay = gameplay
        verdict = self.evaluate_runtime_conditions()
        if verdict.blocked:
            self.pause_safe_stop(verdict.detail or verdict.reason.value if verdict.reason else "")
            return verdict
        self.actions_used = 0
        self.activated_mono_ms = time.monotonic() * 1000.0
        self._to(ExecutionState.ACTIVE, "all gates passed")
        return BlockResult.allow()

    def complete(self) -> bool:
        return self._to(ExecutionState.COMPLETED, "scenario finished; input locked")

    def pause_safe_stop(self, reason: str) -> bool:
        """Any failed condition. Releases input, cancels the queue, never refocuses."""
        return self._to(ExecutionState.PAUSED_SAFE_STOP, reason or "safety condition failed")

    def emergency_stop(self, reason: str = "user pressed emergency stop") -> bool:
        """Strongest stop. Requires a completely fresh arm flow afterwards."""
        ok = self._to(ExecutionState.EMERGENCY_STOP, reason)
        if not ok:
            # From OFF/OBSERVE there is no legal path; force the state and release anyway.
            self.state = ExecutionState.EMERGENCY_STOP
            self.history.append({"from": self.state.value, "to": "emergency_stop",
                                 "reason": f"forced: {reason}", "mono_ms": 0.0})
        self.void_arm(f"emergency stop: {reason}")
        self._release()
        return ok

    # ------------------------------------------------------- the eleven gates

    def evaluate_runtime_conditions(self) -> BlockResult:
        """Every condition that must hold for input, checked explicitly.

        Ordered cheapest-first. Each check records its own result so a refusal says exactly
        which condition failed rather than "something went wrong".
        """
        checks: dict[str, str] = {}

        def deny(reason: BlockReason, detail: str) -> BlockResult:
            checks[reason.value] = detail
            return BlockResult.deny(reason, detail)

        # 1. No emergency stop.
        if self.state is ExecutionState.EMERGENCY_STOP:
            return deny(BlockReason.ESTOP_ACTIVE, "emergency stop is latched")

        # 2. Armed by the user, with a live token bound to this session.
        token = self.arm_token
        if token is None or not token.valid:
            return deny(BlockReason.NO_ARM_TOKEN,
                        token.voided_reason if token else "no arm token")

        # 3. A registered target session.
        if self.session is None:
            return deny(BlockReason.SESSION_MISSING, "no registered target session")
        hwnd = int(getattr(self.session, "hwnd", 0) or 0)
        pid = int(getattr(self.session, "pid", 0) or 0)
        if hwnd <= 0:
            return deny(BlockReason.TARGET_HWND_INVALID, "session has no hwnd")

        # 4. Never the agent's own process or window.
        if pid == self.self_pid or (self.self_hwnd and hwnd == self.self_hwnd):
            return deny(BlockReason.PROTECTED_AT_POINT,
                        "the registered target is Frame Forge itself")

        # 5. Target identity still matches.
        from frameforge.actions.target import window_identity, window_root

        live = window_identity(hwnd)
        if not live or not live.get("title") and not live.get("class_name"):
            return deny(BlockReason.TARGET_HWND_INVALID, f"window {hwnd} no longer exists")
        if int(live.get("pid", 0)) != pid:
            return deny(BlockReason.TARGET_PROCESS_MISMATCH,
                        f"window {hwnd} is now pid {live['pid']}, expected {pid}")

        # 6. Capture fresh and healthy, and bound to this session.
        capture = self.last_capture
        if capture is None:
            return deny(BlockReason.CAPTURE_UNAVAILABLE, "no capture for this session")
        ok, why = capture.usable()
        if not ok:
            return deny(BlockReason.CAPTURE_UNAVAILABLE, why)
        if capture.target_hwnd != hwnd or capture.target_pid != pid:
            return deny(BlockReason.CAPTURE_STALE,
                        f"capture is for hwnd={capture.target_hwnd}, "
                        f"target is hwnd={hwnd}")
        checks["capture"] = f"{capture.age_ms:.0f}ms old, healthy"

        # 7. Display topology unchanged since registration.
        fp = getattr(self.session, "topology_fingerprint", "")
        if fp and capture.topology_fingerprint and capture.topology_fingerprint != fp:
            return deny(BlockReason.TOPOLOGY_CHANGED,
                        "display topology changed since the target was registered")

        # 8. Foreground policy. The agent must not be foreground.
        from frameforge.actions.safety import read_foreground_window

        fg = read_foreground_window()
        if fg is None:
            return deny(BlockReason.TARGET_NOT_FOREGROUND, "no foreground window")
        fg_root = window_root(int(fg.hwnd))
        if int(fg.pid) == self.self_pid:
            return deny(BlockReason.SELF_FOREGROUND,
                        "Frame Forge is the foreground window; the user must return focus "
                        "to the game")
        if fg_root != hwnd and int(fg.pid) != pid:
            return deny(BlockReason.TARGET_NOT_FOREGROUND,
                        f"foreground is hwnd={fg_root} pid={fg.pid} "
                        f"({fg.process_name}), not the registered target")

        # 9. Gameplay: user confirmed, and the machine verifier agrees.
        if not token.user_attestation.strip():
            return deny(BlockReason.GAMEPLAY_UNCONFIRMED,
                        "the user has not confirmed active gameplay")
        if self.gameplay.state not in INPUT_PERMITTING_GAMEPLAY:
            return deny(BlockReason.GAMEPLAY_STATE_BLOCKED,
                        f"game state is {self.gameplay.state.value}")
        if not self.gameplay.permits_input:
            return deny(BlockReason.GAMEPLAY_STATE_BLOCKED,
                        f"gameplay confidence {self.gameplay.confidence:.2f} is insufficient")
        checks["gameplay"] = f"{self.gameplay.state.value} @ {self.gameplay.confidence:.2f}"

        # 10. Duration limit.
        if self.activated_mono_ms:
            active_ms = time.monotonic() * 1000.0 - self.activated_mono_ms
            limit = float(getattr(self.profile, "duration_limit_ms", 60_000.0))
            if active_ms > limit:
                return deny(BlockReason.DEADLINE_EXCEEDED,
                            f"active for {active_ms:.0f}ms, limit {limit:.0f}ms")

        # 11. Action budget.
        limit = int(getattr(self.profile, "max_actions", 200))
        if self.actions_used >= limit:
            return deny(BlockReason.DEADLINE_EXCEEDED,
                        f"action budget exhausted ({self.actions_used}/{limit})")

        result = BlockResult.allow()
        self._last_checks = checks
        return result

    # ------------------------------------------------------------ authorisation

    def authorize(self, intent: str, *, point: Any = None, session_id: str | None = None) -> BlockResult:
        """The single gate for live input. Everything else must call this."""
        checks: dict[str, str] = {}

        # 1. State. ACTIVE is the only state that permits input.
        if self.state not in INPUT_PERMITTING_STATES:
            return BlockResult.deny(BlockReason.NOT_ARMED,
                                   f"state is {self.state.value}; input is locked")

        # 2. Runtime conditions.
        runtime = self.evaluate_runtime_conditions()
        if runtime.blocked:
            self.pause_safe_stop(runtime.detail)
            return runtime

        # 3. The token must be bound to this session, not merely present.
        token = self.arm_token
        if token is None:
            return BlockResult.deny(BlockReason.NO_ARM_TOKEN, "no arm token")
        if session_id is not None and token.session_id and token.session_id != session_id:
            return BlockResult.deny(BlockReason.TARGET_NOT_REGISTERED,
                                    "arm token belongs to a different session")

        # 4. The profile must permit this specific intent.
        profile = self.profile
        if profile is None:
            return BlockResult.deny(BlockReason.PROFILE_FORBIDS_ACTION, "no input profile")
        ok, why = profile.permits(intent)
        if not ok:
            return BlockResult.deny(BlockReason.PROFILE_FORBIDS_ACTION, why)

        self.actions_used += 1
        return BlockResult.allow()

    # ----------------------------------------------------------------- reporting

    def status(self) -> dict[str, Any]:
        """Everything a banner needs, and everything a report records."""
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "input_locked": self.state not in INPUT_PERMITTING_STATES,
            "arm_token": self.arm_token.to_dict() if self.arm_token else None,
            "profile": self.profile.name if self.profile else None,
            "profile_detail": self.profile.to_dict() if self.profile else None,
            "session": getattr(self.session, "to_dict", lambda: None)() if self.session else None,
            "capture": self.last_capture.to_dict() if self.last_capture else None,
            "gameplay": self.gameplay.to_dict(),
            "actions_used": self.actions_used,
            "transitions": self.history[-12:],
        }

    def banner(self) -> str:
        """The always-visible line while armed or active."""
        if self.state not in (ExecutionState.ARMED_COUNTDOWN, ExecutionState.ACTIVE,
                              ExecutionState.PAUSED_SAFE_STOP):
            return "INPUT LOCKED"
        target = getattr(self.session, "title", "") or "?"
        profile = self.profile.name if self.profile else "?"
        label = {ExecutionState.ARMED_COUNTDOWN: "ARMED",
                 ExecutionState.ACTIVE: "ACTIVE",
                 ExecutionState.PAUSED_SAFE_STOP: "PAUSED"}[self.state]
        return (f"INPUT {label}  |  Target: {target}  |  Profile: {profile}  |  "
                f"Actions: {self.actions_used}")


__all__ = [
    "ArmToken",
    "BlockReason",
    "BlockResult",
    "CaptureFrame",
    "DEFAULT_EARLY_GAMEPLAY_PROFILE",
    "ExecutionDecision",
    "ExecutionState",
    "ExecutionStateMachine",
    "GameplayState",
    "GameplayVerdict",
    "INPUT_PERMITTING_GAMEPLAY",
    "INPUT_PERMITTING_STATES",
    "InputProfile",
    "SYSTEM_INTENTS",
]
