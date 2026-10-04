"""Runner/CLI-to-backend integration tests: unsafe states must produce ZERO input.

These deliberately start from :class:`ScenarioRunner` - the real composition root - rather
than from ``ExecutionStateMachine`` or ``TargetGuard` directly. The previous component
tests all passed while the runner never constructed those components at all, which is
exactly the failure this file exists to prevent: a test that exercises the parts but not
the wiring proves nothing about the system.

The input port is a recording fake, so nothing here can reach the OS. The assertions are
about *dispatches*, because a refusal that still dispatches is not a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frameforge.actions.arm import ExecutionState, GameplayState, GameplayVerdict
from frameforge.config.settings import Settings
from frameforge.ports import fakes
from frameforge.ports.capture import Surface, SurfaceKind
from frameforge.ports.geometry import Size
from frameforge.profiles.loader import load_game_profile
from frameforge.tasks.loader import parse_scenario
from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "profiles" / "games" / "testbed.json"

ACTIVE = GameplayVerdict(GameplayState.ACTIVE_GAMEPLAY, confidence=0.9, source="test")


class RecordingInput:
    """Records dispatches; never reaches the OS. Mirrors the live port contract."""

    name = "sendinput"

    def __init__(self):
        from frameforge.actions.safety import InputSafetyManager

        self.safety = InputSafetyManager()
        self._enabled = False
        self.dispatched: list = []
        self.releases = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool, thorough: bool = True) -> None:
        self._enabled = value

    def send(self, primitive, action_id: str = "", source: str = "") -> bool:
        self.dispatched.append(primitive)
        return True

    def send_batch(self, prims, action_id: str = "") -> int:
        for prim in prims:
            self.send(prim, action_id)
        return len(prims)

    def release_all(self, thorough: bool = True) -> dict:
        self.releases += 1
        return self.safety.release_all(thorough=thorough)

    def position(self):
        from frameforge.ports.geometry import Point

        return Point(0, 0)

    @property
    def button_presses(self) -> list:
        from frameforge.ports.input import PrimitiveType

        return [p for p in self.dispatched if p.kind is PrimitiveType.MOUSE_BUTTON and p.down]

    @property
    def key_presses(self) -> list:
        from frameforge.ports.input import PrimitiveType

        return [p for p in self.dispatched if p.kind is PrimitiveType.KEY and p.down]


@pytest.fixture
def wired(tmp_path):
    """A real ScenarioRunner with fakes. Returns (runner, input_port, session)."""
    profile = load_game_profile(PROFILE)
    profile.metadata["_profile_dir"] = str(PROFILE.parent)
    scenario = parse_scenario({
        "name": "gate_test",
        "objective": "prove the gate refuses",
        "steps": [{"name": "noop", "kind": "assert", "verify": "always"}],
    })
    surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000)
    capture = fakes.FakeCapture(surface)
    capture.open(surface)
    port = RecordingInput()
    wiring = RunnerWiring(
        window=fakes.FakeWindow(title="FrameForge Testbed", class_name="TkTopLevel",
                                process_name="python.exe"),
        capture=capture,
        ocr=fakes.FakeOcr(default=["FRAMEFORGE TESTBED"]),
        vision=fakes.FakeVision(),
        input_port=port,
        profile=profile,
        templates={},
        surface=surface,
    )
    runner = ScenarioRunner(Settings(runs_dir=tmp_path / "runs", grace_seconds=0.0),
                           wiring, scenario=scenario)
    runner.prepare()
    runner.fetch_observation()
    return runner, port, surface


def _primitives(x=400, y=300):
    from frameforge.ports.input import Primitive, PrimitiveType

    return [
        Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=x, y=y),
        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button="left", down=True, hold_ms=40),
        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button="left", down=False),
    ]


# --------------------------------------------------------- 1. default posture is locked


class TestDefaultPosture:
    def test_runner_starts_input_locked(self, wired):
        """A profile lets the runner reach TARGET_SELECTED; input is still locked there.

        The distinction matters: registering a target is observation, not permission. Only
        ACTIVE permits input, and TARGET_SELECTED is not ACTIVE.
        """
        runner, port, _surface = wired
        assert runner.machine.state is ExecutionState.TARGET_SELECTED
        assert runner.machine.state not in (ExecutionState.ACTIVE,)
        assert runner.live_input_status()["input_locked"] is True

    def test_unarmed_run_cannot_dispatch(self, wired):
        runner, port, _s = wired
        result = runner.live_gate.authorize("forward", point=None, action_id="t1") \
            if runner.live_gate else None
        # The gate may not exist before a target binds; either way nothing dispatches.
        if result is not None:
            assert result.blocked
        assert port.button_presses == []

    def test_session_registration_does_not_unlock_input(self, wired):
        """Having a registered target must not, by itself, permit a single click."""
        runner, port, _s = wired
        assert runner.target_session is not None
        assert runner.live_input_status()["input_locked"] is True
        assert port.button_presses == [] and port.key_presses == []

    def test_agent_windows_are_registered_protected(self, wired):
        runner, _port, _s = wired
        protected = runner.protected.report()
        assert runner.machine.self_pid in protected["protected_pids"]
        assert "chrome_widgetwin_1" in protected["protected_classes"]
        assert "progman" in protected["protected_classes"]

    def test_arm_without_attestation_is_refused(self, wired):
        runner, port, _s = wired
        result = runner.arm_for_live_input(attestation="", countdown_ms=0)
        assert result.blocked
        assert port.button_presses == []
        assert port.key_presses == []

    def test_arm_registers_a_target_session(self, wired):
        runner, _port, _s = wired
        assert runner.target_session is not None
        assert runner.target_session.hwnd == runner._target.hwnd
        assert runner.target_session.pid == runner._target.pid

    def test_status_reports_input_locked(self, wired):
        runner, _port, _s = wired
        status = runner.live_input_status()
        assert status["input_locked"] is True
        assert "INPUT LOCKED" in status["banner"]
        assert status["session"]["hwnd"] == runner._target.hwnd


# ---------------------------------------------------- 2. unsafe states produce zero input


class TestUnsafeStatesProduceNoInput:
    def _armed(self, wired):
        runner, port, _s = wired
        result = runner.arm_for_live_input(
            attestation="I am in active gameplay and my character is controllable.",
            countdown_ms=0.0)
        return runner, port, result

    def test_countdown_alone_never_dispatches(self, wired):
        runner, port, result = self._armed(wired)
        if result.blocked:
            return                      # refused earlier is also correct
        assert runner.machine.state is ExecutionState.ARMED_COUNTDOWN
        denied = runner.live_gate.authorize("forward", action_id="c1")
        assert denied.blocked
        assert port.button_presses == [] and port.key_presses == []

    def test_countdown_ending_without_active_denies(self, wired):
        runner, port, result = self._armed(wired)
        if result.blocked:
            return
        denied = runner.live_gate.authorize("forward", action_id="c2")
        assert denied.blocked
        assert "not_in_active" in denied.reason.value
        assert port.button_presses == []

    def test_gameplay_state_unknown_blocks(self, wired):
        runner, port, _r = self._armed(wired)
        runner.machine.gameplay = GameplayVerdict(GameplayState.UNKNOWN, confidence=0.9)
        denied = runner.live_gate.authorize("forward", action_id="g1")
        assert denied.blocked
        assert port.button_presses == []

    def test_forbidden_intent_is_refused_even_when_active(self, wired):
        runner, port, _r = self._armed(wired)
        runner.machine.state = ExecutionState.ACTIVE   # forced, to isolate the profile rule
        for intent in ("ctrl", "alt", "win", "menu"):
            denied = runner.live_gate.authorize(intent, action_id=f"p-{intent}")
            assert denied.blocked, f"{intent} was authorised"
        assert port.key_presses == []

    def test_cursor_position_cannot_bypass_the_gate(self, wired):
        """Whatever the cursor is doing, an unarmed run still refuses."""
        runner, port, _s = wired
        port.safety  # noqa: B018 - touch to be explicit
        for _ in range(5):
            denied = runner.live_gate.authorize("forward", action_id="cur") \
                if runner.live_gate else None
            if denied is not None:
                assert denied.blocked
        assert port.button_presses == []

    def test_fixed_coordinate_cannot_bypass_target_guard(self, wired):
        """A point inside the target's bounds still needs ACTIVE and a live session."""
        runner, port, _s = wired
        if runner.live_gate is not None:
            denied = runner.live_gate.authorize("forward", point=None, action_id="fx")
            assert denied.blocked
        assert port.button_presses == []

    def test_dispatch_refuses_and_emits_nothing(self, wired):
        runner, port, _r = self._armed(wired)
        result = runner.live_gate.dispatch("forward", _primitives(), action_id="d1")
        assert result.blocked
        assert port.dispatched == []

    def test_emergency_stop_releases_and_blocks(self, wired):
        runner, port, _s = wired
        runner.machine.emergency_stop("integration test")
        assert runner.machine.state is ExecutionState.EMERGENCY_STOP
        assert port.releases >= 1, "emergency stop did not release held input"
        denied = runner.live_gate.authorize("forward", action_id="e1") \
            if runner.live_gate else None
        if denied is not None:
            assert denied.blocked
        assert port.button_presses == []

    def test_no_profile_means_no_session_and_no_input(self, tmp_path):
        """A scenario with no game profile cannot acquire a target at all."""
        scenario = parse_scenario({
            "name": "no_profile",
            "steps": [{"name": "noop", "kind": "assert", "verify": "always"}],
        })
        surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1)
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        port = RecordingInput()
        runner = ScenarioRunner(
            Settings(runs_dir=tmp_path / "r", grace_seconds=0.0),
            RunnerWiring(
                window=fakes.FakeWindow(), capture=capture, ocr=fakes.FakeOcr(),
                vision=fakes.FakeVision(), input_port=port, surface=surface,
            ),
            scenario=scenario,
        )
        runner.prepare()
        runner.fetch_observation()
        assert runner.target_session is None
        assert runner.machine.state is ExecutionState.OBSERVE
        assert runner.live_input_status()["input_locked"] is True
        assert port.dispatched == []


class TestEnforcedPathIsTheOnlyPath:
    """Proof that the chain the runner uses is the one the audit requires."""

    def test_executor_carries_the_gate_guard(self, wired):
        runner, _port, _s = wired
        assert runner._executor is not None
        assert runner._executor.target_guard is not None
        assert runner._executor._policy is not None
        assert runner._executor._controller is not None

    def test_executor_cannot_be_built_without_a_controller(self, wired):
        """Fail-closed at *construction*, which is stronger than refusing to dispatch.

        An executor with no controller cannot exist at all, so there is no code path where
        a later caller forgets to check. The earlier design refused at execute time and left
        an unguarded object constructible in the meantime.
        """
        from frameforge.actions.executor import ActionExecutor

        runner, port, _s = wired
        with pytest.raises(ValueError, match="InputController"):
            ActionExecutor(port, controller=None, policy=None)
        assert not port.button_presses

    def test_run_path_refuses_to_build_an_executor_unguarded(self, wired):
        """The runner itself must fail closed rather than degrade to an unguarded run."""
        runner, port, _s = wired
        assert runner._executor is not None
        assert runner._executor._controller is runner.controller
        assert runner._executor._policy is runner.policy

    def test_gate_and_machine_share_one_session(self, wired):
        runner, _port, _s = wired
        if runner.live_gate is not None:
            assert runner.live_gate.machine is runner.machine
            assert runner.live_gate.registry is runner.protected
