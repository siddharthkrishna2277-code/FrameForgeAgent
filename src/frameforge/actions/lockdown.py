"""Unconditional live-input lockdown.

A live run performed an unauthorized, account-level side effect: a click intended for a
Notepad document on the external monitor landed inside the Hermes window on the laptop
monitor and activated a paid model link. The project owner received a confirmation email.

That is a safety incident, not a test failure. Until the injection path is rebuilt and
re-authorised by the owner, no live desktop input may be emitted.

**Where this gate sits.** Below everything: below the planner, the executor, the controller,
the policy, the profile, the scenario, the CLI, and the test helpers. It is enforced at the
*ctypes binding* for ``user32.SendInput`` and ``SetCursorPos``, so it holds no matter what
any caller above does — misconfigured guard, bypassed session, wrong guardrail, or a bug in a
new code path. There is no layer above it that can talk to the OS directly.

**Default state is locked.** Nothing has to be enabled, unlocked, or configured for this to
be in force; the inverse would be the failure mode.

**Recovery of held input is exempt and still runs.** ``release_all`` deliberately bypasses
the arm/disarm gate, because blocking a release strands a key physically down on the
operator's machine. A lockdown that refuses to release is more dangerous than the incident
that caused it. Lockdown therefore blocks *injection of intent* while remaining able to
*undo what was already done*.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

#: The single reason string reported for every refused injection. Stable, greppable, and
#: carries its own cause so an operator reading a log knows why nothing happened.
LOCKDOWN_REASON = "LIVE_INPUT_LOCKDOWN_AFTER_UNAUTHORIZED_SIDE_EFFECT"

@dataclass
class LockdownState:
    active: bool = True
    reason: str = LOCKDOWN_REASON
    refused_primitives: int = 0
    refused_calls: int = 0
    last_refused_kind: str = ""
    notes: list[str] = field(default_factory=list)


_state = LockdownState()
_lock = threading.Lock()


def lockdown_active() -> bool:
    """True when OS input emission is prohibited.

    Locked by default and locked unconditionally in this build. There is no environment
    variable, config key, CLI flag, profile field, or model instruction that lifts it: an
    emergency gate that can be opened by a setting is not a gate, it is a suggestion. The
    owner re-authorises live input by changing code, in a reviewed commit, which is an
    auditable act rather than a runtime toggle.
    """
    with _lock:
        return _state.active


def record_refusal(kind: str) -> None:
    """Note a refused injection. Called from the OS boundary, so it cannot be skipped."""
    with _lock:
        _state.refused_primitives += 1
        _state.refused_calls += 1
        _state.last_refused_kind = str(kind)


def state() -> dict[str, object]:
    with _lock:
        return {
            "LIVE_INPUT": "LOCKED_DOWN_AFTER_UNAUTHORIZED_SIDE_EFFECT"
            if _state.active else "UNLOCKED_BY_OWNER_TOKEN",
            "active": _state.active,
            "reason": _state.reason,
            "refused_primitives": _state.refused_primitives,
            "refused_os_calls": _state.refused_calls,
            "last_refused_kind": _state.last_refused_kind,
            "notes": list(_state.notes),
        }


#: Result shape required of every injector under lockdown.
LOCKDOWN_RESULT: dict[str, object] = {
    "input_emitted": False,
    "reason": LOCKDOWN_REASON,
}


class LiveInputLocked(RuntimeError):
    """Raised at the OS boundary when injection is refused.

    A distinct type, rather than returning 0, because every caller checks the returned
    count and would read 0 as "SendInput failed" - producing a spurious OSError and a
    misleading audit record on every single refused primitive. The refusal is a policy
    outcome, not a device failure.
    """

    def __init__(self, kind: str = "") -> None:
        super().__init__(f"{LOCKDOWN_REASON}: {kind}" if kind else LOCKDOWN_REASON)
        self.kind = kind


def refuses(releasing: bool) -> bool:
    """Whether an emission of this shape must be refused.

    ``releasing`` escapes for one reason only: a stuck modifier on the operator's machine is
    a worse outcome than the incident that produced this module, and a release cannot cause
    the side effect that triggered the lockdown.
    """
    if releasing:
        return False
    return lockdown_active()


__all__ = [
    "LOCKDOWN_REASON",
    "LiveInputLocked",
    "LOCKDOWN_RESULT",
    "lockdown_active",
    "record_refusal",
    "refuses",
    "state",
]
