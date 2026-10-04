"""Clock port implementations.

Two clocks, deliberately separated:

* ``monotonic_ms`` - durations, timeouts, latency accounting. Never affected by wall
  clock changes or NTP steps.
* ``wall_ms``      - human-facing timestamps in evidence and reports.

Tests inject ``FakeClock`` so that timeout and rate-limit logic is exercised
deterministically and instantly rather than by sleeping (guardrail: no test may need a
real display, and by extension no test should need real time).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    """Time source. Implemented by :class:`SystemClock` and :class:`FakeClock`."""

    def monotonic_ms(self) -> float:
        """Milliseconds from an arbitrary fixed origin. Monotonic."""
        ...

    def wall_ms(self) -> float:
        """Milliseconds since the Unix epoch. For display only."""
        ...

    def sleep_ms(self, ms: float) -> None:
        """Block for ``ms`` milliseconds."""
        ...

    def iso_now(self) -> str:
        """UTC ISO-8601 timestamp with millisecond precision."""
        ...


class SystemClock:
    """Real clock backed by :mod:`time`."""

    __slots__ = ()

    def monotonic_ms(self) -> float:
        return time.monotonic() * 1000.0

    def wall_ms(self) -> float:
        return time.time() * 1000.0

    def sleep_ms(self, ms: float) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)

    def iso_now(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")


class FakeClock:
    """Deterministic clock for tests.

    ``sleep_ms`` advances virtual time instead of blocking, so a test can exercise a
    500 ms debounce or a 3-second timeout in microseconds.
    """

    __slots__ = ("_mono", "_wall", "_sleep_calls")

    def __init__(self, start_monotonic_ms: float = 0.0, start_wall_ms: float = 1_700_000_000_000.0):
        self._mono = start_monotonic_ms
        self._wall = start_wall_ms
        self._sleep_calls: list[float] = []

    def monotonic_ms(self) -> float:
        return self._mono

    def wall_ms(self) -> float:
        return self._wall

    def sleep_ms(self, ms: float) -> None:
        if ms < 0:
            msg = f"negative sleep: {ms}"
            raise ValueError(msg)
        self._sleep_calls.append(ms)
        self._mono += ms
        self._wall += ms

    def advance_ms(self, ms: float) -> None:
        """Move virtual time forward without recording a sleep (models real waiting)."""
        self._mono += ms
        self._wall += ms

    @property
    def sleep_calls(self) -> list[float]:
        return list(self._sleep_calls)

    def iso_now(self) -> str:
        return datetime.fromtimestamp(self._wall / 1000.0, tz=UTC).isoformat(timespec="milliseconds")


__all__ = ["ClockPort", "FakeClock", "SystemClock"]