"""Run state machine.

Only :class:`frameforge.kernel.director.RunDirector` may transition state. Every
transition is emitted as an event so the timeline in a report is authoritative rather
than reconstructed.

``PAUSED`` is not a failure. A run pauses when the human touches the machine, when the
display topology changes, or when the target window needs re-acquisition, then resumes
after re-verifying the last postcondition.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    """Lifecycle states of a single run."""

    IDLE = "idle"
    ARMING = "arming"
    ACQUIRING_TARGET = "acquiring_target"
    OBSERVING = "observing"
    DECIDING = "deciding"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"

    PAUSED_FOCUS = "paused_focus"
    PAUSED_USER = "paused_user"
    PAUSED_HEALTH = "paused_health"

    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    ABORTED = "aborted"
    #: Input cleanup could not be verified. Deliberately *not* a success state: the run
    #: did its work, but the operator's keyboard may not be their own any more, and that
    #: must never be reported as a clean finish.
    CLEANUP_FAILED = "cleanup_failed"


TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.UNKNOWN, RunState.ABORTED,
     RunState.CLEANUP_FAILED}
)

PAUSED_STATES: frozenset[RunState] = frozenset(
    {RunState.PAUSED_FOCUS, RunState.PAUSED_USER, RunState.PAUSED_HEALTH}
)

ACTIVE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.OBSERVING,
        RunState.DECIDING,
        RunState.EXECUTING,
        RunState.VERIFYING,
        RunState.RECOVERING,
    }
)

#: States in which the loop may send input. Anything else means the input ports are
#: no-ops (guardrail G-SES-01, TEST-SES-01). Note ARMING is deliberately absent: the
#: grace countdown happens before ARMING completes.
INPUT_PERMITTED_STATES: frozenset[RunState] = frozenset({RunState.EXECUTING})


class TransitionError(RuntimeError):
    """An illegal state transition was attempted."""


_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.IDLE: frozenset({RunState.ARMING, RunState.ABORTED}),
    RunState.ARMING: frozenset({RunState.ACQUIRING_TARGET, RunState.ABORTED, RunState.FAILED}),
    RunState.ACQUIRING_TARGET: frozenset(
        {
            RunState.OBSERVING,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_HEALTH,
            RunState.FAILED,
            RunState.ABORTED,
        }
    ),
    RunState.OBSERVING: frozenset(
        {
            RunState.DECIDING,
            # A verify-only step has nothing to decide; it goes straight to verification.
            RunState.VERIFYING,
            # A run whose last step ended cleanly finishes from here.
            RunState.COMPLETED,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_USER,
            RunState.PAUSED_HEALTH,
            RunState.ABORTED,
            RunState.FAILED,
        }
    ),
    RunState.DECIDING: frozenset(
        {
            RunState.EXECUTING,
            RunState.RECOVERING,
            RunState.COMPLETED,
            RunState.UNKNOWN,
            RunState.FAILED,
            RunState.ABORTED,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_USER,
            RunState.PAUSED_HEALTH,
        }
    ),
    RunState.EXECUTING: frozenset(
        {
            RunState.VERIFYING,
            RunState.RECOVERING,
            RunState.OBSERVING,
            RunState.ABORTED,
            RunState.FAILED,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_USER,
            RunState.PAUSED_HEALTH,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.OBSERVING,
            RunState.RECOVERING,
            RunState.DECIDING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.UNKNOWN,
            RunState.ABORTED,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_USER,
        }
    ),
    RunState.RECOVERING: frozenset(
        {
            RunState.OBSERVING,
            RunState.DECIDING,
            RunState.EXECUTING,
            RunState.FAILED,
            RunState.UNKNOWN,
            RunState.ABORTED,
            RunState.PAUSED_FOCUS,
            RunState.PAUSED_HEALTH,
        }
    ),
    RunState.PAUSED_FOCUS: frozenset(
        {
            RunState.ACQUIRING_TARGET,
            RunState.OBSERVING,
            RunState.ABORTED,
            RunState.FAILED,
        }
    ),
    # Resuming after a human-input pause goes through re-acquisition like any other pause,
    # so that the focus guard is re-checked before anything is sent again.
    RunState.PAUSED_USER: frozenset(
        {RunState.ACQUIRING_TARGET, RunState.ARMING, RunState.ABORTED, RunState.UNKNOWN}
    ),
    RunState.PAUSED_HEALTH: frozenset(
        {RunState.ACQUIRING_TARGET, RunState.OBSERVING, RunState.ABORTED, RunState.FAILED}
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.UNKNOWN: frozenset(),
    RunState.ABORTED: frozenset(),
    RunState.CLEANUP_FAILED: frozenset(),
}


def can_transition(current: RunState, nxt: RunState) -> bool:
    return nxt in _ALLOWED[current]


def assert_transition(current: RunState, nxt: RunState) -> None:
    if not can_transition(current, nxt):
        msg = f"illegal transition {current} -> {nxt}"
        raise TransitionError(msg)


__all__ = [
    "ACTIVE_STATES",
    "INPUT_PERMITTED_STATES",
    "PAUSED_STATES",
    "TERMINAL_STATES",
    "RunState",
    "TransitionError",
    "assert_transition",
    "can_transition",
]