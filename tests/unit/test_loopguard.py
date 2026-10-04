"""Loop hygiene and orthogonal outcomes, adopted from dsh's guard family.

Tests the guard's behaviour and, more importantly, the property that makes it safe to
adopt: it is advisory. A guard that silently refuses actions strands runs in a state
neither the operator nor the report explains.
"""

from __future__ import annotations

from frameforge.actions.executor import ExecutionResult
from frameforge.actions.loopguard import LoopGuard, LoopSignal
from frameforge.actions.model import Click, Intent, KeyPress
from frameforge.kernel.clock import FakeClock
from frameforge.ports.geometry import Point


class TestLoopGuard:
    def test_clean_run_reports_nothing(self):
        guard = LoopGuard(clock=FakeClock())
        guard.begin_step()
        for action in (Click(at=Point(1, 1)), Click(at=Point(2, 2)), Intent(intent="jump")):
            assert guard.observe(action).ok
        assert guard.report()["findings"] == []

    def test_repetition_is_detected(self):
        guard = LoopGuard(clock=FakeClock(), repeat_threshold=3)
        guard.begin_step()
        for _ in range(3):
            verdict = guard.observe(Click(at=Point(5, 5)))
        assert verdict.signal is LoopSignal.REPEATED
        assert verdict.should_stop_retrying
        assert guard.repeated_count == 1

    def test_different_actions_are_not_repetition(self):
        """Tapping the same UI element twice is legitimate; the guard must not flag it."""
        guard = LoopGuard(clock=FakeClock(), repeat_threshold=3)
        guard.begin_step()
        for action in (Click(at=Point(1, 1)), Click(at=Point(9, 9)), Click(at=Point(4, 4))):
            assert guard.observe(action).signal is not LoopSignal.REPEATED

    def test_progress_resets_the_repeat_counter(self):
        """After progress, a fresh run of the same action starts counting again.

        The point is that repetition is counted *within* a window of no progress. Three
        identical actions spread across two successful steps is normal behaviour; three in a
        row within one stalled step is the failure the guard exists to surface.
        """
        guard = LoopGuard(clock=FakeClock(), repeat_threshold=3)
        guard.begin_step()
        for _ in range(2):
            guard.observe(Click(at=Point(1, 1)))
        guard.note_progress()
        guard.begin_step()
        # Count restarts: two more, and only the third is repetition.
        verdicts = [guard.observe(Click(at=Point(1, 1))) for _ in range(3)]
        assert verdicts[0].signal is not LoopSignal.REPEATED
        assert verdicts[1].signal is not LoopSignal.REPEATED
        assert verdicts[2].signal is LoopSignal.REPEATED

    def test_different_points_are_different_signatures(self):
        """Regression: the signature must include coordinates.

        The first implementation used the action's one-line description, which omits the
        click point, so three clicks at *different* places looked identical and the guard
        would have mis-flagged any scenario that taps several UI elements.
        """
        guard = LoopGuard(clock=FakeClock(), repeat_threshold=3)
        guard.begin_step()
        for x, y in ((1, 1), (2, 2), (3, 3)):
            assert guard.observe(Click(at=Point(x, y))).signal is not LoopSignal.REPEATED

    def test_action_deadline_is_flagged(self):
        clock = FakeClock()
        guard = LoopGuard(clock=clock, action_deadline_ms=100.0)
        guard.begin_step()
        assert guard.observe(Wait()).ok
        clock.advance_ms(500)
        verdict = guard.observe(Wait())
        assert verdict.signal is LoopSignal.OVER_DEADLINE
        assert verdict.should_stop_retrying

    def test_deadline_can_be_disabled(self):
        clock = FakeClock()
        guard = LoopGuard(clock=clock, action_deadline_ms=None)
        guard.begin_step()
        clock.advance_ms(10_000)
        assert guard.observe(Click(at=Point(1, 1))).ok

    def test_findings_are_recorded_not_raised(self):
        """Advisory by design: never blocks, always visible."""
        guard = LoopGuard(clock=FakeClock(), repeat_threshold=2)
        guard.begin_step()
        guard.observe(Click(at=Point(2, 2)))
        guard.observe(Click(at=Point(2, 2)))
        assert guard.findings
        assert "should_stop_retrying" in guard.findings[0]

    def test_report_is_serialisable(self):
        import json

        guard = LoopGuard(clock=FakeClock())
        guard.begin_step()
        guard.observe(Click(at=Point(1, 1)))
        json.dumps(guard.report())

    def test_verdict_to_dict_is_complete(self):
        verdict = LoopGuard(clock=FakeClock()).observe(Click(at=Point(1, 1)))
        for key in ("signal", "detail", "repeats", "should_stop_retrying"):
            assert key in verdict.to_dict()


def Wait():
    from frameforge.actions.model import Wait as _W

    return _W(ms=10)


class TestOrthogonalOutcomes:
    """A single boolean cannot distinguish four different things.

    dsh's defensive-patterns rule: "never nest one flag's report inside another's branch,
    or a caller reads a cut-short run as a clean success."
    """

    def test_clean_execution(self):
        r = ExecutionResult(primitives_sent=3)
        assert r.ok and r.outcome == "executed"
        assert not r.denied and not r.partial and not r.error

    def test_denied_sends_nothing(self):
        r = ExecutionResult(primitives_sent=0, blocked_reason="denied: refused", denied=True)
        assert not r.ok and r.outcome == "denied"
        assert r.primitives_sent == 0

    def test_partial_is_distinguishable_from_denied(self):
        """The bug this prevents: 'sent 3 then blocked' reported as 0 looks like a no-op."""
        r = ExecutionResult(primitives_sent=3, blocked_reason="focus lost", partial=True)
        assert not r.ok and r.outcome == "partial"
        assert r.primitives_sent == 3, "the true count must survive a partial batch"

    def test_error_is_distinguishable_from_block(self):
        r = ExecutionResult(primitives_sent=2, error="TimeoutError: x")
        assert r.outcome == "error" and r.error

    def test_all_four_outcomes_are_distinct(self):
        outcomes = {
            ExecutionResult(primitives_sent=1).outcome,
            ExecutionResult(primitives_sent=0, denied=True).outcome,
            ExecutionResult(primitives_sent=2, partial=True).outcome,
            ExecutionResult(primitives_sent=0, error="boom").outcome,
        }
        assert len(outcomes) == 4
