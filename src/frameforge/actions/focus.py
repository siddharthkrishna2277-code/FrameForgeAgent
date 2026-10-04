"""Focus guard and human-input detection.

This module is the difference between an agent and a hazard on a machine with one
mouse.

:class:`FocusGuard` is checked before **every** input batch. If the foreground window is
no longer the target, input is not sent. That single check prevents the worst realistic
failure: the user alt-tabs away mid-run and the agent keeps clicking at coordinates that
now mean something entirely different.

:class:`HumanInputDetector` watches ``GetLastInputInfo``. The subtlety is that Frame
Forge's *own* synthetic input resets that counter, so a naive "did input happen" check
reports the agent's own clicks as human activity. The detector therefore samples a
baseline *before* each action and looks for a jump afterwards, which cannot be produced
by input the agent itself sent.

Default policy on detected human input is pause-and-hand-back, not "compete for the
mouse" (guardrail G-SES-03).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import FocusLostError, HumanInputDetected, SessionInactiveError
from frameforge.ports.window import SessionState, WindowInfo, WindowPort


class HumanInputPolicy(StrEnum):
    """What to do when the human touches the machine."""

    PAUSE = "pause"      # default: stop, hand back, wait (G-SES-03)
    RESUME = "resume"    # keep going after a grace period
    IGNORE = "ignore"    # only for supervised unattended runs; never the default


class FocusPolicy(StrEnum):
    """What to do when focus drifts."""

    PAUSE = "pause"        # default: pause, re-acquire, re-verify (G-SES-02)
    ABORT = "abort"
    REFOCUS = "refocus"    # explicitly take focus back; requires opt-in


@dataclass(slots=True)
class FocusState:
    """Snapshot of focus, for the timeline and the health log."""

    target_hwnd: int | None = None
    foreground_hwnd: int | None = None
    ok: bool = True
    detail: str = ""
    drift_since_ms: float | None = None


class FocusGuard:
    """Blocks input unless the foreground window is the target."""

    def __init__(
        self,
        window: WindowPort,
        target_hwnd: int | None = None,
        *,
        policy: FocusPolicy = FocusPolicy.PAUSE,
        tolerance_ms: float = 250.0,
        clock: ClockPort | None = None,
    ) -> None:
        self._window = window
        self._target_hwnd = target_hwnd
        self._policy = policy
        self._tolerance_ms = tolerance_ms
        self._clock = clock or SystemClock()
        self._drift_since: float | None = None
        self._drift_count = 0
        #: Whether the target has ever been seen correctly focused in this run. The
        #: tolerance window may only absorb *transitions*, so it is only granted after a
        #: correct focus has actually been observed.
        self._ever_focused = False

    @property
    def policy(self) -> FocusPolicy:
        return self._policy

    def set_target(self, hwnd: int | None) -> None:
        self._target_hwnd = hwnd
        self._drift_since = None

    def check_session(self) -> SessionState:
        """Confirm the session is interactive before any input.

        Injecting input into a locked or disconnected session is the case with nobody
        present to press estop, so it is refused outright (G-SES-01).
        """
        state = self._window.session_state()
        if not state.interactive:
            msg = f"session not interactive: {state}"
            raise SessionInactiveError(msg)
        return state

    def current_state(self) -> FocusState:
        fg = self._window.foreground()
        fg_hwnd = fg.hwnd if fg else None
        ok = self._target_hwnd is not None and fg_hwnd == self._target_hwnd
        if ok:
            self._drift_since = None
            self._ever_focused = True
        else:
            now = self._clock.monotonic_ms()
            if self._drift_since is None:
                self._drift_since = now
                self._drift_count += 1
        drift_for = None if self._drift_since is None else self._clock.monotonic_ms() - self._drift_since
        return FocusState(
            target_hwnd=self._target_hwnd,
            foreground_hwnd=fg_hwnd,
            ok=ok,
            detail="ok" if ok else f"foreground {fg_hwnd} != target {self._target_hwnd}",
            drift_since_ms=drift_for,
        )

    def assert_focus(self) -> None:
        """Raise unless focus is correct and stable.

        Called immediately before every input batch. The tolerance window absorbs the
        ordinary flicker when a window is being activated; a genuine user alt-tab holds
        focus elsewhere far longer than 250 ms.
        """
        self.check_session()
        state = self.current_state()
        if state.ok:
            return
        if (
            state.drift_since_ms is not None
            and state.drift_since_ms < self._tolerance_ms
            and self._ever_focused
            and self._policy is not FocusPolicy.ABORT
        ):
            # Brief flicker *after* a correct focus was observed: this is the ordinary
            # window-activation case, and blocking a legitimate click behind it would make
            # the guard unusable.
            #
            # The `_ever_focused` condition is load-bearing. Without it, a target that is
            # *already* unfocused when the guard is created gets a 250 ms grace on its
            # first check - meaning the first input of a run is sent to a window the user
            # has already switched away from. That was a real defect, found by the
            # focus-loss integration test.
            return
        msg = f"focus lost: {state.detail}"
        raise FocusLostError(msg)

    def try_refocus(self) -> bool:
        """Attempt to reclaim focus. Only under an explicit REFOCUS policy."""
        if self._target_hwnd is None or self._policy is not FocusPolicy.REFOCUS:
            return False
        ok = self._window.set_foreground(self._target_hwnd)
        if ok:
            self._drift_since = None
            self._ever_focused = True
        return ok

    @property
    def drift_count(self) -> int:
        return self._drift_count


class HumanInputDetector:
    """Detects human input, ignoring Frame Forge's own.

    The baseline-then-compare approach is what makes this work: sample idle time before
    an action, sample after, and only a *jump* beyond what the agent itself could have
    caused counts as human.
    """

    def __init__(
        self,
        window: WindowPort,
        *,
        policy: HumanInputPolicy = HumanInputPolicy.PAUSE,
        grace_ms: int = 500,
        clock: ClockPort | None = None,
    ) -> None:
        self._window = window
        self._policy = policy
        self._grace_ms = grace_ms
        self._clock = clock or SystemClock()
        self._baseline_idle = window.idle_ms()
        self._last_human_ms: float | None = None
        self.human_events = 0

    @property
    def policy(self) -> HumanInputPolicy:
        return self._policy

    def mark_agent_input(self) -> None:
        """Call immediately after the agent sends input.

        Rebases the baseline so the agent's own events are never mistaken for a human.
        """
        self._baseline_idle = self._window.idle_ms()

    def check(self) -> bool:
        """Return True when human input is detected. Updates the baseline if not.

        Uses the ``ignore_threshold``: idle time that jumps by less than
        ``grace_ms`` between two samples is ordinary jitter from our own sends, not a
        human pressing something.
        """
        current = self._window.idle_ms()
        # idle_ms resets to 0 on any input. If the baseline was high (quiet for a
        # while) and now it is low, a human acted.
        human = self._baseline_idle > self._grace_ms and current <= self._grace_ms
        if human:
            self.human_events += 1
            self._last_human_ms = self._clock.monotonic_ms()
        else:
            # Advance the baseline only when it grew, so we never lose a pending signal.
            self._baseline_idle = max(current, self._baseline_idle if human else current)
            if current > self._baseline_idle:
                self._baseline_idle = current
        return human

    def assert_no_human_input(self) -> None:
        if self.check():
            if self._policy is HumanInputPolicy.PAUSE:
                msg = f"human input detected ({self.human_events} events); pausing"
                raise HumanInputDetected(msg)
            if self._policy is HumanInputPolicy.IGNORE:
                return

    @property
    def last_human_ms(self) -> float | None:
        return self._last_human_ms


__all__ = [
    "FocusGuard",
    "FocusPolicy",
    "FocusState",
    "HumanInputDetector",
    "HumanInputPolicy",
]
