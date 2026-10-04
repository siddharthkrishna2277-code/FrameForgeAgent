"""Recovery ladder.

Seven rungs, each budgeted, escalating only when the previous one has been tried and has
failed. Every rung terminates, and rung 7 ends the run with a complete evidence bundle.
There is no rung that loops (guardrail G-ABS-07).

  1. retry the action with jittered backoff (<= 2)
  2. re-verify perception; re-acquire window and focus
  3. dismiss a known modal via a profile reaction rule
  4. bounded deterministic probe, seeded
  5. ask tier-1 AI for a recovery plan (if enabled and budget remains)
  6. mark the step UNKNOWN and move to the next independent step
  7. abort the run

Rung 5 is the only one that can involve AI, and it is optional: the ladder reaches rung 7
with ``ai: off`` without changing shape. That is what makes the AI tier genuinely
optional rather than load-bearing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import IntEnum

from frameforge.kernel.clock import ClockPort, SystemClock


class Rung(IntEnum):
    RETRY = 1
    REVERIFY = 2
    DISMISS_MODAL = 3
    PROBE = 4
    ASK_AI = 5
    SKIP_UNKNOWN = 6
    ABORT = 7


RUNG_NAMES: dict[Rung, str] = {
    Rung.RETRY: "retry with backoff",
    Rung.REVERIFY: "re-verify perception and re-acquire target",
    Rung.DISMISS_MODAL: "dismiss known modal",
    Rung.PROBE: "bounded deterministic probe",
    Rung.ASK_AI: "ask tier-1 planner",
    Rung.SKIP_UNKNOWN: "mark step UNKNOWN and continue",
    Rung.ABORT: "abort run",
}


@dataclass(slots=True)
class RecoveryPolicy:
    """Rung budgets. Kept small on purpose: escalation should be rare."""

    max_retries: int = 2
    base_backoff_ms: int = 200
    max_backoff_ms: int = 2_000
    #: One reverify and one modal dismissal are almost always enough.
    max_reverify: int = 1
    max_modal_dismiss: int = 2
    max_probes: int = 3
    #: Whether rung 5 is available. False when the AI tier is off.
    ai_enabled: bool = False
    max_ai_attempts: int = 1
    #: Whether to continue past a step that could not be reached.
    allow_skip: bool = True


@dataclass(slots=True)
class RecoveryAttempt:
    """One rung invocation, for the audit trail."""

    rung: Rung
    ok: bool = False
    detail: str = ""
    mono_ms: float = 0.0


@dataclass(slots=True)
class RecoveryLadder:
    """Stateful recovery for one step."""

    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    clock: ClockPort = field(default_factory=SystemClock)
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    _counts: dict[Rung, int] = field(default_factory=dict)
    _seed: int = 0

    def reset(self, seed: int = 0) -> None:
        self._counts.clear()
        self.attempts.clear()
        self._seed = seed

    @property
    def exhausted(self) -> bool:
        """True when only ABORT remains."""
        return self.next_rung() is Rung.ABORT

    def next_rung(self) -> Rung | None:
        """The next rung available, or None when the ladder is spent.

        Returns rungs in order, skipping any whose budget is used up, and returning None
        only when even SKIP and ABORT have been consumed - which cannot happen for a
        fresh ladder, so the caller is guaranteed to terminate.
        """
        p = self.policy
        checks = (
            (Rung.RETRY, self._counts.get(Rung.RETRY, 0) < p.max_retries),
            (Rung.REVERIFY, self._counts.get(Rung.REVERIFY, 0) < p.max_reverify),
            (Rung.DISMISS_MODAL, self._counts.get(Rung.DISMISS_MODAL, 0) < p.max_modal_dismiss),
            (Rung.PROBE, self._counts.get(Rung.PROBE, 0) < p.max_probes),
            (Rung.ASK_AI, p.ai_enabled and self._counts.get(Rung.ASK_AI, 0) < p.max_ai_attempts),
        )
        for rung, available in checks:
            if available:
                return rung
        if self._counts.get(Rung.SKIP_UNKNOWN, 0) == 0 and p.allow_skip:
            return Rung.SKIP_UNKNOWN
        return Rung.ABORT

    def consume(self, rung: Rung, ok: bool, detail: str = "") -> RecoveryAttempt:
        self._counts[rung] = self._counts.get(rung, 0) + 1
        attempt = RecoveryAttempt(rung=rung, ok=ok, detail=detail, mono_ms=self.clock.monotonic_ms())
        self.attempts.append(attempt)
        return attempt

    def backoff_ms(self, attempt: int) -> float:
        """Jittered exponential backoff for the retry rung.

        Jitter is here to avoid two *rungs* colliding with a game's own timers, not to
        evade detection - there is no behavioural-evasion goal in this system
        (guardrail G-ABS-03).
        """
        p = self.policy
        base = min(p.max_backoff_ms, p.base_backoff_ms * (2**attempt))
        rng = random.Random(self._seed + attempt)
        return base * (0.75 + rng.random() * 0.5)

    def summary(self) -> str:
        if not self.attempts:
            return "no recovery needed"
        return "; ".join(
            f"{RUNG_NAMES.get(a.rung, str(a.rung))}:{'ok' if a.ok else 'failed'}" for a in self.attempts
        )


__all__ = ["RUNG_NAMES", "RecoveryAttempt", "RecoveryLadder", "RecoveryPolicy", "Rung"]
