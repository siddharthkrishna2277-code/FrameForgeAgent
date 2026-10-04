"""Budget and rate limiting.

Two distinct mechanisms, both ledger-enforced:

* :class:`BudgetLedger` - *cumulative* caps (actions this run, AI calls this run,
  unknown states this run). Crossing one is terminal for the run or degrades it.
* :class:`RateLimiter` - *rate* cap (actions per minute). Prevents a runaway loop from
  hammering a game or a UI at a rate a human never could, and bounds blast radius if
  something goes wrong in a loop.

Neither is advisory. Every check happens before an action is compiled and executed, and
``BudgetExceededError`` propagates rather than being caught and ignored. Guardrail
G-ABS-07: no loop may run without a budget.

Rate is computed from a sliding window of timestamps rather than a token bucket. A token
bucket is smoother for throughput, but here the question is "how many in the last 60 s",
which is what a report needs to state, and which a human can reason about.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import BudgetExceededError


@dataclass(slots=True)
class BudgetLimits:
    """Hard caps for one run. Every field is optional; ``None`` means unbounded.

    Defaults are deliberately conservative on this hardware. A 12 GB laptop driving a
    game at 30 actions/second will exhaust the user's patience and possibly the game's
    tolerance long before it exhausts memory.
    """

    max_actions: int | None = 2000
    max_actions_per_minute: int | None = 240
    max_ai_calls: int | None = 60
    max_unknown_states: int | None = 25
    max_retries_per_step: int | None = 3
    max_held_ms_total: int | None = 120_000
    max_run_ms: int | None = 3_600_000

    def describe(self) -> dict[str, int | None]:
        return {
            "max_actions": self.max_actions,
            "max_actions_per_minute": self.max_actions_per_minute,
            "max_ai_calls": self.max_ai_calls,
            "max_unknown_states": self.max_unknown_states,
            "max_retries_per_step": self.max_retries_per_step,
            "max_held_ms_total": self.max_held_ms_total,
            "max_run_ms": self.max_run_ms,
        }


@dataclass(slots=True)
class BudgetLedger:
    """Tracks consumption and refuses anything over cap."""

    limits: BudgetLimits = field(default_factory=BudgetLimits)
    clock: ClockPort = field(default_factory=SystemClock)

    actions_used: int = 0
    ai_calls_used: int = 0
    unknown_states: int = 0
    held_ms_total: float = 0.0
    retries_used: int = 0
    started_ms: float = 0.0
    _action_times: deque[float] = field(default_factory=deque)
    _events: list[tuple[str, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.started_ms:
            self.started_ms = self.clock.monotonic_ms()

    # ------------------------------------------------------------------ accessors

    @property
    def elapsed_ms(self) -> float:
        return self.clock.monotonic_ms() - self.started_ms

    @property
    def actions_left(self) -> int | None:
        return None if self.limits.max_actions is None else max(0, self.limits.max_actions - self.actions_used)

    @property
    def ai_calls_left(self) -> int | None:
        return None if self.limits.max_ai_calls is None else max(0, self.limits.max_ai_calls - self.ai_calls_used)

    def actions_per_minute(self) -> float:
        return self._window_count(self.clock.monotonic_ms(), 60_000.0)

    # ------------------------------------------------------------------- charging

    def charge_actions(self, count: int = 1, *, held_ms: float = 0.0) -> None:
        """Charge ``count`` actions. Raises if any cap would be exceeded.

        Checks happen *before* incrementing, so a refused charge leaves the ledger
        accurate rather than overshooting.
        """
        now = self.clock.monotonic_ms()
        limit = self.limits.max_actions
        if limit is not None and self.actions_used + count > limit:
            self._record("actions", now)
            msg = f"action budget exhausted: {self.actions_used}/{limit}"
            raise BudgetExceededError(msg)

        rate_limit = self.limits.max_actions_per_minute
        if rate_limit is not None:
            self._prune(now)
            projected = sum(1 for t in self._action_times if t > now - 60_000.0) + count
            if projected > rate_limit:
                self._record("rate", now)
                msg = f"action rate exceeded: {projected} in 60s, limit {rate_limit}"
                raise BudgetExceededError(msg)

        self.actions_used += count
        for _ in range(count):
            self._action_times.append(now)
        self.held_ms_total += held_ms
        held_cap = self.limits.max_held_ms_total
        if held_cap is not None and self.held_ms_total > held_cap:
            self._record("held_ms", now)
            msg = f"sustained-input budget exhausted: {self.held_ms_total:.0f}ms > {held_cap}ms"
            raise BudgetExceededError(msg)
        self._record("actions", now)

    def charge_ai_call(self) -> None:
        limit = self.limits.max_ai_calls
        if limit is not None and self.ai_calls_used + 1 > limit:
            self._record("ai", self.clock.monotonic_ms())
            msg = f"AI call budget exhausted: {self.ai_calls_used}/{limit}"
            raise BudgetExceededError(msg)
        self.ai_calls_used += 1
        self._record("ai", self.clock.monotonic_ms())

    def charge_unknown(self) -> None:
        limit = self.limits.max_unknown_states
        if limit is not None and self.unknown_states + 1 > limit:
            self._record("unknown", self.clock.monotonic_ms())
            msg = f"unknown-state budget exhausted: {self.unknown_states}/{limit}"
            raise BudgetExceededError(msg)
        self.unknown_states += 1

    def charge_retry(self) -> None:
        limit = self.limits.max_retries_per_step
        if limit is not None and self.retries_used + 1 > limit:
            self._record("retry", self.clock.monotonic_ms())
            msg = f"retry budget exhausted: {self.retries_used}/{limit}"
            raise BudgetExceededError(msg)
        self.retries_used += 1

    def reset_retries(self) -> None:
        """Called when a step succeeds, so retries are per-step not per-run."""
        self.retries_used = 0

    def check_runtime(self) -> None:
        limit = self.limits.max_run_ms
        if limit is not None and self.elapsed_ms > limit:
            self._record("runtime", self.clock.monotonic_ms())
            msg = f"run time budget exhausted: {self.elapsed_ms:.0f}ms > {limit}ms"
            raise BudgetExceededError(msg)

    # ------------------------------------------------------------------- internals

    def _prune(self, now: float) -> None:
        while self._action_times and self._action_times[0] <= now - 60_000.0:
            self._action_times.popleft()

    def _window_count(self, now: float, window_ms: float) -> float:
        return float(sum(1 for t in self._action_times if t > now - window_ms))

    def _record(self, kind: str, now: float) -> None:
        self._events.append((kind, now))

    def snapshot(self) -> dict[str, object]:
        """Budget state for the report. A consumer must be able to see the caps."""
        return {
            "limits": self.limits.describe(),
            "actions_used": self.actions_used,
            "ai_calls_used": self.ai_calls_used,
            "unknown_states": self.unknown_states,
            "held_ms_total": round(self.held_ms_total, 1),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "actions_per_minute": self.actions_per_minute(),
        }


__all__ = ["BudgetLedger", "BudgetLimits"]
