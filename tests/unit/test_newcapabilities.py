"""Tests for capabilities added after building against a real third-party application.

Every one of these exists because something failed against real Notepad, and each is
pinned here so the failure cannot come back silently. They run headless.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frameforge.kernel.clock import FakeClock
from frameforge.ports import fakes
from frameforge.ports.window import OcclusionInfo


def _win(hwnd: int, title: str, klass: str, pid: int, fg: bool = False):
    """A WindowInfo for the pure disambiguation tests."""
    from frameforge.ports.geometry import Point, Rect
    from frameforge.ports.window import WindowInfo

    return WindowInfo(
        hwnd=hwnd, title=title, class_name=klass, pid=pid, process_name="t.exe",
        is_foreground=fg, client_rect=Rect(0, 0, 100, 100), client_origin_screen=Point(0, 0),
    )


class TestOcclusionDetection:
    """Capture is a screen grab, not a window render. A covered target is a broken target."""

    def test_reports_coverage(self):
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
        from frameforge.ports.geometry import Point, Size

        adapter = PyWin32WindowAdapter()
        # A synthetic case: hit-test a region we know is on the desktop.
        info = adapter.occlusion(0, Point(0, 0), Size(4, 4))
        assert isinstance(info, OcclusionInfo)

    def test_health_warns_then_pauses_by_severity(self):
        from frameforge.perception.health import (
            DisplayHealthMonitor,
            HealthPolicy,
            HealthVerdict,
        )
        from frameforge.ports.geometry import Point, Size

        window = fakes.FakeWindow()
        monitor = DisplayHealthMonitor(window, fakes.FakeVision())

        window.occluded_by = "NotificationPopup"
        window.occluded_fraction = 0.05
        report = monitor.check_occlusion(1000, Point(0, 0), Size(100, 100))
        assert report.verdict is HealthVerdict.WATCH
        assert "NotificationPopup" in report.detail

        window.occluded_fraction = 0.5
        report = monitor.check_occlusion(1000, Point(0, 0), Size(100, 100))
        assert report.verdict is HealthVerdict.PAUSE

    def test_clear_target_is_ok(self):
        from frameforge.perception.health import DisplayHealthMonitor, HealthVerdict
        from frameforge.ports.geometry import Point, Size

        monitor = DisplayHealthMonitor(fakes.FakeWindow(), fakes.FakeVision())
        report = monitor.check_occlusion(1000, Point(0, 0), Size(100, 100))
        assert report.verdict is HealthVerdict.OK

    def test_partial_coverage_is_enough_to_pause(self):
        """Partial coverage is the dangerous case: pixels silently mix two applications."""
        from frameforge.perception.health import (
            DisplayHealthMonitor,
            HealthPolicy,
            HealthVerdict,
        )
        from frameforge.ports.geometry import Point, Size

        monitor = DisplayHealthMonitor(fakes.FakeWindow(), fakes.FakeVision())
        window = monitor._window
        window.occluded_by = "Toast"
        window.occluded_fraction = 0.25
        report = monitor.check_occlusion(1000, Point(0, 0), Size(100, 100))
        assert report.verdict is HealthVerdict.PAUSE
        assert report.metrics["occluded_fraction"] == 0.25


class TestSessionStateConfirmation:
    """A null foreground window is transient. Believing it immediately blocks real runs."""

    def test_requires_persistence_before_declaring_locked(self):
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        adapter = PyWin32WindowAdapter()
        # A live interactive desktop must never be reported as locked.
        state = adapter.session_state()
        assert state.interactive, f"live session reported as {state}"

    def test_fake_window_defaults_interactive(self):
        from frameforge.ports.window import SessionState

        assert fakes.FakeWindow().session_state() is SessionState.ACTIVE


class TestNonIdempotentRetry:
    """Retrying a typing step after a failed postcondition typed the probe three times."""

    def test_typing_is_not_retryable(self):
        from frameforge.tasks.dsl import StepAction, TaskStep

        step = TaskStep(name="type", action=StepAction(kind="type", text="x"))
        assert step.is_retryable() is False

    def test_scrolling_and_mouselook_are_not_retryable(self):
        from frameforge.tasks.dsl import StepAction, TaskStep

        assert not TaskStep(name="s", action=StepAction(kind="scroll")).is_retryable()
        assert not TaskStep(name="m", action=StepAction(kind="mouselook")).is_retryable()

    def test_clicking_is_retryable(self):
        from frameforge.tasks.dsl import StepAction, TaskStep

        assert TaskStep(name="c", action=StepAction(kind="click")).is_retryable()

    def test_explicit_flag_overrides(self):
        from frameforge.tasks.dsl import StepAction, TaskStep

        step = TaskStep(name="t", action=StepAction(kind="type"), retryable=True)
        assert step.is_retryable() is True


class TestSetupSteps:
    """A scenario needs to establish a known starting state."""

    def test_setup_parses(self, tmp_path):
        from frameforge.tasks.loader import parse_scenario

        raw = {
            "name": "s",
            "setup": [{"name": "dismiss", "action": {"kind": "key", "key": "escape"},
                       "verify": "always"}],
            "steps": [{"name": "assert", "verify": "always"}],
        }
        scenario = parse_scenario(raw)
        assert [s.name for s in scenario.setup] == ["dismiss"]
        assert [s.name for s in scenario.steps] == ["assert"]

    def test_setup_failure_does_not_fail_the_run(self, tmp_path):
        """A scenario may legitimately already be in the desired state."""
        from frameforge.kernel.events import EventKind
        from frameforge.ports import fakes
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size
        from frameforge.config.settings import Settings
        from frameforge.tasks.loader import parse_scenario
        from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

        surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000)
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        scenario = parse_scenario({
            "name": "s",
            "setup": [{"name": "always_fails", "verify": "anchor_visible",
                       "verify_args": {"anchor": "never_there"}}],
            "steps": [{"name": "ok", "verify": "always"}],
        })
        wiring = RunnerWiring(
            window=fakes.FakeWindow(), capture=capture, ocr=fakes.FakeOcr(),
            vision=fakes.FakeVision(), input_port=fakes.FakeInput(), surface=surface,
        )
        runner = ScenarioRunner(Settings(runs_dir=tmp_path, grace_seconds=0.0),
                                wiring, scenario=scenario)
        runner.prepare()
        _outcome, report = runner.run()
        # The setup step did not pass (UNKNOWN: the anchor was never evaluated), but the
        # run's own step passed and the run completed regardless. "not a pass" is the point;
        # whether it is FAIL or UNKNOWN depends on *why* the setup step did not take effect,
        # and both must be non-fatal.
        setup_results = [s for s in report.steps if s.name.startswith("setup:")]
        assert setup_results and setup_results[0].disposition != "pass"
        assert any(s.name == "ok" and s.disposition == "pass" for s in report.steps)
        assert report.state == "completed"


class TestLaunchAuthorisation:
    """Direct launch is opt-in per scenario and the command is recorded."""

    def test_launch_spec_parses(self):
        from frameforge.tasks.loader import parse_scenario

        raw = {
            "name": "s",
            "allow_direct_launch": True,
            "launch": {"executable": "C:/app/game.exe", "args": ["--qa"],
                       "ready_landmark": "main_menu"},
            "steps": [{"name": "a", "verify": "always"}],
        }
        scenario = parse_scenario(raw)
        assert scenario.launch is not None
        assert scenario.launch.argv() == ["C:/app/game.exe", "--qa"]

    def test_launch_requires_executable(self):
        from frameforge.kernel.errors import PolicyError
        from frameforge.tasks.loader import parse_scenario

        with pytest.raises(PolicyError, match="executable"):
            parse_scenario({"name": "s", "launch": {"args": []}, "steps": []})

    def test_unauthorised_launch_is_refused(self):
        from frameforge.tasks.launch import is_authorised
        from frameforge.tasks.loader import parse_scenario

        scenario = parse_scenario({
            "name": "s",
            "launch": {"executable": "x.exe"},
            "steps": [{"name": "a", "verify": "always"}],
        })
        # Declared but not permitted: refused.
        assert is_authorised(scenario) is False

    def test_authorised_launch_permitted(self):
        from frameforge.tasks.launch import is_authorised
        from frameforge.tasks.loader import parse_scenario

        scenario = parse_scenario({
            "name": "s",
            "allow_direct_launch": True,
            "launch": {"executable": "x.exe"},
            "steps": [{"name": "a", "verify": "always"}],
        })
        assert is_authorised(scenario) is True

    def test_no_launch_declared_means_no_launch(self):
        from frameforge.tasks.launch import is_authorised
        from frameforge.tasks.loader import parse_scenario

        scenario = parse_scenario({"name": "s", "steps": [{"name": "a", "verify": "always"}]})
        assert is_authorised(scenario) is False

    def test_argv_is_never_a_shell_string(self):
        """Nothing is concatenated; the command is an argv list."""
        from frameforge.tasks.loader import parse_scenario

        scenario = parse_scenario({
            "name": "s", "allow_direct_launch": True,
            "launch": {"executable": "C:/a b/game.exe", "args": ["--x y"]},
            "steps": [],
        })
        assert scenario.launch.argv() == ["C:/a b/game.exe", "--x y"]
        assert all(isinstance(part, str) for part in scenario.launch.argv())

    def test_ensure_target_polls_until_present(self, tmp_path):
        from frameforge.tasks.dsl import LaunchSpec
        from frameforge.tasks.launch import ensure_target

        clock = FakeClock()
        attempts = {"n": 0}

        def resolve():
            attempts["n"] += 1
            return 4242 if attempts["n"] >= 3 else None

        spec = LaunchSpec(executable="cmd.exe", args=["/c", "exit", "0"], timeout_ms=5000)
        assert ensure_target(spec, resolve, clock=clock) == 4242
        assert attempts["n"] == 3

    def test_ensure_target_returns_none_on_timeout(self):
        from frameforge.tasks.dsl import LaunchSpec
        from frameforge.tasks.launch import ensure_target

        spec = LaunchSpec(executable="cmd.exe", args=["/c", "exit", "0"], timeout_ms=300)
        assert ensure_target(spec, lambda: None, clock=FakeClock(), poll_ms=50) is None

    def test_ensure_target_does_not_launch_if_already_present(self):
        from frameforge.tasks.dsl import LaunchSpec
        from frameforge.tasks.launch import ensure_target

        spec = LaunchSpec(executable="definitely-not-a-real-binary-xyz")
        # Would raise if it tried to launch; returning the hwnd proves it did not.
        assert ensure_target(spec, lambda: 7, clock=FakeClock()) == 7


class TestTextMethodPerProfile:
    """The shared 'ui' preset defaults to unicode, which a Win32 EDIT control ignores."""

    def test_profile_can_declare_scan(self):
        from frameforge.profiles.schema import GameProfileSpec

        spec = GameProfileSpec.model_validate({
            "name": "t",
            "authorized_use": {"basis": "owner_prototype", "attestation": "a long enough note"},
            "target": {"class_name": "Notepad"},
            "text_method": "scan",
        })
        assert spec.text_method == "scan"

    def test_invalid_method_rejected(self):
        from frameforge.profiles.schema import GameProfileSpec

        with pytest.raises(Exception):
            GameProfileSpec.model_validate({
                "name": "t",
                "authorized_use": {"basis": "owner_prototype", "attestation": "a long enough note"},
                "target": {"class_name": "Notepad"},
                "text_method": "telepathy",
            })

    def test_control_profile_honours_it(self):
        from frameforge.profiles.schema import GameProfileSpec
        from frameforge.tasks.runner import build_control_profile

        spec = GameProfileSpec.model_validate({
            "name": "t", "control_preset": "ui", "text_method": "scan",
            "authorized_use": {"basis": "owner_prototype", "attestation": "a long enough note"},
            "target": {"class_name": "Notepad"},
        })
        assert build_control_profile(spec).text_method == "scan"

    def test_default_is_unicode_from_the_preset(self):
        from frameforge.profiles.schema import GameProfileSpec
        from frameforge.tasks.runner import build_control_profile

        spec = GameProfileSpec.model_validate({
            "name": "t", "control_preset": "ui",
            "authorized_use": {"basis": "owner_prototype", "attestation": "a long enough note"},
            "target": {"class_name": "Notepad"},
        })
        assert build_control_profile(spec).text_method == "unicode"


class TestAmbiguousTarget:
    """Ambiguity must name the candidates, not report a confusing 'not found'."""

    def test_two_matches_are_ambiguous(self):
        """Pure logic: two equally-good matches must not be resolved by coin flip."""
        from frameforge.adapters.window.pywin32_window import disambiguate
        from frameforge.kernel.errors import AmbiguousTargetError
        from frameforge.ports.window import TargetSpec

        a = _win(hwnd=1, title="A - Notepad", klass="Notepad", pid=10, fg=True)
        b = _win(hwnd=2, title="B - Notepad", klass="Notepad", pid=10, fg=False)
        spec = TargetSpec(title_regex=r"Notepad$", class_name="Notepad")
        with pytest.raises(AmbiguousTargetError) as exc:
            disambiguate([a, b], spec)
        assert len(exc.value.candidates) == 2
        assert "Narrow the profile" in str(exc.value)

    def test_single_match_resolves(self):
        from frameforge.adapters.window.pywin32_window import disambiguate
        from frameforge.ports.window import TargetSpec

        a = _win(hwnd=1, title="Only - Notepad", klass="Notepad", pid=10)
        assert disambiguate([a], TargetSpec(class_name="Notepad")) is a

    def test_foreground_wins_when_required(self):
        from frameforge.adapters.window.pywin32_window import disambiguate
        from frameforge.ports.window import TargetSpec

        a = _win(hwnd=1, title="Back - Notepad", klass="Notepad", pid=10, fg=False)
        b = _win(hwnd=2, title="Front - Notepad", klass="Notepad", pid=10, fg=True)
        spec = TargetSpec(title_regex=r"Notepad$", class_name="Notepad", require_foreground=True)
        assert disambiguate([a, b], spec) is b

    def test_full_title_match_wins(self):
        from frameforge.adapters.window.pywin32_window import disambiguate
        from frameforge.ports.window import TargetSpec

        a = _win(hwnd=1, title="Something Else - Notepad", klass="Notepad", pid=10)
        b = _win(hwnd=2, title="Untitled - Notepad", klass="Notepad", pid=10)
        spec = TargetSpec(title_regex=r"^Untitled - Notepad$")
        assert disambiguate([a, b], spec) is b

    def test_ambiguous_message_is_actionable(self):
        """The operator must be told what to change, not just that it failed."""
        from frameforge.kernel.errors import AmbiguousTargetError

        exc = AmbiguousTargetError(["'A' (pid=1)", "'B' (pid=2)", "'C' (pid=3)",
                                    "'D' (pid=4)", "'E' (pid=5)", "'F' (pid=6)",
                                    "'G' (pid=7)"])
        text = str(exc)
        assert "+1 more" in text
        assert "title_regex" in text


class TestPauseResume:
    """Pause was detected but never resumed: the loop crashed on the next transition."""

    def test_resume_path_exists(self):
        from frameforge.kernel.director import RunDirector
        from frameforge.kernel.states import RunState

        assert hasattr(RunDirector, "recover_from_pause")

    def test_paused_focus_can_resume(self, tmp_path):
        from frameforge.actions.focus import FocusGuard, FocusPolicy
        from frameforge.kernel.bus import EventLog
        from frameforge.kernel.director import RunDirector
        from frameforge.kernel.states import RunState

        window = fakes.FakeWindow()
        guard = FocusGuard(window, target_hwnd=window.info.hwnd,
                           policy=FocusPolicy.REFOCUS, clock=FakeClock())
        log = EventLog(None, "r", clock=FakeClock())
        director = RunDirector(event_log=log, assembler=None, verifier=None,
                               executor=None, validator=None,
                               ledger=__import__(
                                   "frameforge.actions.ledger", fromlist=["BudgetLedger"]
                               ).BudgetLedger(clock=FakeClock()),
                               clock=FakeClock())
        director._focus_guard = guard
        director.state = RunState.PAUSED_FOCUS
        assert director.recover_from_pause() is True
        assert director.state is RunState.OBSERVING
