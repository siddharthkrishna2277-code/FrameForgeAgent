"""Loop-hygiene guards.

Adopted from dsh's ``packages/guard`` family (``repeat-tool-reminder`` and
``timeout-policy``), which exist because those failure modes "actually shipped or nearly
shipped here" in a comparable agent loop.

Both problems are live in Frame Forge and both are currently handled only indirectly:

* A **stuck loop** burns actions, wall-clock and budget while making no progress. The
  budget ledger eventually stops it, but only after the damage; and a run that repeats the
  same failing action produces a report full of identical rows with no explanation.
* A **hung call** stalls a turn. Frame Forge has a run-level timeout and a per-step timeout,
  but no per-*action* deadline, so one blocking call delays the evidence a report needs.

Deliberately advisory rather than blocking. A guard that silently refuses an action can
strand a run in a state neither the operator nor the report explains; these record a fact,
degrade, and stay visible.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from frameforge.kernel.clock import ClockPort, SystemClock


class LoopSignal(StrEnum):
    """What the guard observed."""

    NONE = "none"
    REPEATED = "repeated_action"
    NO_PROGRESS = "no_progress"
    OVER_DEADLINE = "over_deadline"


@dataclass(slots=True)
class GuardVerdict:
    """An advisory finding. Never blocks; always recorded."""

    signal: LoopSignal = LoopSignal.NONE
    detail: str = ""
    #: How many times the same action signature has now been seen.
    repeats: int = 0
    #: True when the guard believes the caller should stop retrying.
    should_stop_retrying: bool = False

    @property
    def ok(self) -> bool:
        return self.signal is LoopSignal.NONE

    def to_dict(self) -> dict[str, object]:
        return {
            "signal": self.signal.value,
            "detail": self.detail,
            "repeats": self.repeats,
            "should_stop_retrying": self.should_stop_retrying,
        }


@dataclass(slots=True)
class LoopGuard:
    """Detects repetition, stalls and no-progress, and records what it saw."""

    #: Identical action signature seen this many times in a row is repetition.
    repeat_threshold: int = 3
    #: Steps since the last verdict change / progress.
    no_progress_threshold: int = 4
    #: Advisory per-action deadline. ``None`` disables it.
    action_deadline_ms: float | None = 30_000.0

    clock: ClockPort = field(default_factory=SystemClock)

    _history: deque = field(default_factory=deque, init=False, repr=False)
    _verdicts: deque = field(default_factory=deque, init=False, repr=False)
    _last_progress_ms: float = field(default=0.0, init=False)
    _step_started_ms: float = field(default=0.0, init=False)
    _step_has_clock: bool = field(default=False, init=False)
    findings: list[dict[str, object]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # Bounded windows: a long run must not grow these without limit on 12 GB of RAM.
        self._history = deque(maxlen=32)
        self._verdicts = deque(maxlen=64)

    def begin_step(self) -> None:
        # Recorded even when the clock reads 0. A clock that has not started yet is exactly
        # when the deadline must still work, so the previous `if self._step_started_ms:`
        # guard silently disabled the deadline for the first step of a run.
        self._step_started_ms = self._clock_ms()
        self._step_has_clock = True

    def _clock_ms(self) -> float:
        clock = self.clock
        return (clock.monotonic_ms() if hasattr(clock, "monotonic_ms")
                else float(clock) * 1000.0)

    @staticmethod
    def signature(action: object) -> str:
        """A comparable form of an action.

        Built from the action's *serialised fields*, not its one-line description. The
        description deliberately omits coordinates and parameters, so three clicks at
        different points all reduced to ``"Click -> 3 primitives"`` and the guard called
        them repetition. A scenario that legitimately taps several UI elements would have
        been mis-flagged on its second distinct click.
        """
        dump = getattr(action, "model_dump", None)
        if callable(dump):
            try:
                return json.dumps(dump(mode="json"), sort_keys=True, default=str)
            except Exception:
                pass
        describe = getattr(action, "describe", None)
        return str(describe() if callable(describe) else action)

    def observe(self, action: object, *, progressed: bool | None = None) -> GuardVerdict:
        """Record an attempt and return an advisory verdict."""
        sig = self.signature(action)
        self._history.append(sig)
        repeats = sum(1 for h in self._history if h == sig)

        if repeats >= self.repeat_threshold:
            verdict = GuardVerdict(
                signal=LoopSignal.REPEATED,
                detail=(f"the same action has been attempted {repeats} times: {sig}"),
                repeats=repeats,
                should_stop_retrying=True,
            )
        else:
            stalled = False
            if self.action_deadline_ms is not None and self._step_has_clock:
                stalled = (self._clock_ms() - self._step_started_ms) > self.action_deadline_ms
            if stalled:
                verdict = GuardVerdict(
                    signal=LoopSignal.OVER_DEADLINE,
                    detail=(f"step exceeded its {self.action_deadline_ms:.0f}ms action "
                            f"deadline"),
                    repeats=repeats,
                    should_stop_retrying=True,
                )
            else:
                verdict = GuardVerdict(repeats=repeats)

        if progressed is True or verdict.repeats == 1:
            self._last_progress_ms = self._clock_ms()

        if not verdict.ok:
            self._verdicts.append(verdict)
            self.findings.append({"mono_ms": round(self._clock_ms(), 2), **verdict.to_dict()})

        return verdict

    def note_progress(self) -> None:
        """A step produced a verdict change: the loop is moving."""
        self._last_progress_ms = self._clock_ms()
        self._history.clear()

    @property
    def repeated_count(self) -> int:
        return sum(1 for v in self._verdicts if v.signal is LoopSignal.REPEATED)

    @property
    def should_stop(self) -> bool:
        return any(v.should_stop_retrying for v in self._verdicts)

    def report(self) -> dict[str, object]:
        return {
            "actions_observed": len(self._history),
            "findings": self.findings[-20:],
            "repeated": self.repeated_count,
            "should_stop_retrying": self.should_stop,
            "action_deadline_ms": self.action_deadline_ms,
        }


__all__ = ["GuardVerdict", "LoopGuard", "LoopSignal"]
