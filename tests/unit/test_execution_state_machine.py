"""The 16 required state-machine tests, against a mock backend.

Each test corresponds to one numbered requirement. The invariant under test throughout is
that *no input exists* unless every gate passes - so most assertions are "zero emissions",
not "correct emissions".
"""

from __future__ import annotations

import time

import pytest

from frameforge.actions.arm import (
    ArmToken,
    BlockReason,
    CaptureFrame,
    DEFAULT_EARLY_GAMEPLAY_PROFILE,
    ExecutionState,
    ExecutionStateMachine,
    GameplayState,
    GameplayVerdict,
    InputProfile,
)
from frameforge.actions.coordinates import SurfacePx
from frameforge.perception.proposal import (
    ProposalType,
    Region,
    VisionProposal,
    to_screen,
    verify_proposal,
)

ACTIVE_GAMEPLAY = GameplayVerdict(GameplayState.ACTIVE_GAMEPLAY, confidence=0.9,
                                 source="test")


class FakeSession:
    """A registered target that never touches the real desktop."""

    def __init__(self, *, hwnd: int = 4242, pid: int = 777,
                 title: str = "Game", process_name: str = "game.exe",
                 client_origin: tuple[int, int] = (1920, 0),
                 client_size=(1920, 1080), topology: str = "fixed") -> None:
        self.session_id = f"sess-{hwnd}"
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.process_name = process_name
        self.image_path = f"C:/games/{process_name}"
        self.class_name = "GameWindow"
        self.client_origin = client_origin
        self.client_size = client_size
        self.monitor_index = 1
        self.monitor_device = "DISPLAY2"
        self.dpi = 96
        self.approved_regions = ((0.0, 0.0, 1.0, 1.0),)
        self.topology_fingerprint = topology
        self.created_mono_ms = time.monotonic() * 1000.0
        self.max_age_ms = 600_000.0
        self.age_ms = 0.0
        self.to_dict = lambda: {"hwnd": hwnd, "pid": pid, "title": title}

    @property
    def client_rect(self):
        from frameforge.ports.geometry import Rect

        return Rect(self.client_origin[0], self.client_origin[1],
                    self.client_size[0], self.client_size[1])


def fresh_capture(session: FakeSession, *, healthy: bool = True, age_ms: float = 0.0) -> CaptureFrame:
    return CaptureFrame(
        frame_id="f1", run_id="r1", session_id=session.session_id,
        target_hwnd=session.hwnd, target_pid=session.pid,
        captured_mono_ms=time.monotonic() * 1000.0 - age_ms,
        max_age_ms=750.0,
        width=session.client_size[0], height=session.client_size[1],
        client_rect=(session.client_origin[0], session.client_origin[1],
                     session.client_size[0], session.client_size[1]),
        topology_fingerprint=session.topology_fingerprint,
        healthy=healthy,
        health_detail="" if healthy else "black frame",
    )


@pytest.fixture
def foreground(monkeypatch):
    """Patch the window boundary the state machine reads.

    Every window call the machine makes goes through ``frameforge.actions.target``; patching
    there means no test can reach a real desktop, and the assertions are about the state
    machine rather than about Win32.

    Returns a setter: ``foreground(hwnd=..., pid=...)``.
    """

    def setter(hwnd: int, pid: int) -> None:
        class FakeWindow:
            pass

        info = FakeWindow()
        info.hwnd = hwnd
        info.pid = pid
        info.title = "foreground"
        info.process_name = "fg.exe"
        info.class_name = "fg"
        # read_foreground_window lives in actions.safety; window_identity/window_root live in
        # actions.target. Patching only the first left the machine reading the *real* desktop,
        # which is how the first version of this fixture silently tested nothing useful.
        monkeypatch.setattr(
            "frameforge.actions.safety.read_foreground_window", lambda: info,
            raising=False)
        monkeypatch.setattr(
            "frameforge.actions.target.read_foreground_window", lambda: info,
            raising=False)
        # window_identity must answer for the *target* hwnd with the registered pid, and for
        # anything else with a different pid. Returning the foreground pid for every hwnd made
        # a foreground change look like a process-identity change, which masked the real
        # refusal reason.
        registered_pid = {"value": 777}

        def fake_identity(h):
            same = int(h) == registered_hwnd["value"]
            return {
                "hwnd": int(h),
                "pid": registered_pid["value"] if same else 999_999,
                "title": "target" if same else "other",
                "class_name": "GameWindow" if same else "Other",
            }

        registered_hwnd = {"value": 4242}
        monkeypatch.setattr("frameforge.actions.target.window_identity",
                            fake_identity, raising=False)
        monkeypatch.setattr("frameforge.actions.target.window_root", lambda h: h,
                            raising=False)

    return setter


def armed_machine(*, countdown_ms: float = 0.0):
    """A machine already walked to ARMED_COUNTDOWN with a valid token."""
    session = FakeSession()
    machine = ExecutionStateMachine(run_id="r1")
    machine.observe()
    machine.select_target(session, DEFAULT_EARLY_GAMEPLAY_PROFILE)
    machine.mark_ready(fresh_capture(session))
    token = ArmToken(
        session_id=session.session_id,
        profile_name=DEFAULT_EARLY_GAMEPLAY_PROFILE.name,
        user_attestation="I am in active gameplay and my character is controllable.",
    )
    machine.begin_countdown(token, countdown_ms=countdown_ms)
    return machine, session, token


# ------------------------------------------------------------------ 1..6


class TestStatesThatCannotInject:
    def test_1_startup_is_off_and_cannot_inject(self):
        machine = ExecutionStateMachine(run_id="r1")
        assert machine.state is ExecutionState.OFF
        result = machine.authorize("forward")
        assert result.blocked
        assert result.reason is BlockReason.NOT_ARMED

    def test_2_observe_cannot_inject(self):
        machine = ExecutionStateMachine(run_id="r1")
        machine.observe()
        assert machine.state is ExecutionState.OBSERVE
        assert machine.authorize("forward").blocked

    def test_3_process_visible_without_arm_cannot_inject(self):
        machine, session, _token = armed_machine(countdown_ms=60000.0)
        # A detected window and a valid session are not an arm.
        assert machine.state is ExecutionState.ARMED_COUNTDOWN
        assert machine.authorize("forward").blocked

    def test_4_selected_target_without_gameplay_confirmation_cannot_inject(self, foreground):
        """A valid session, a valid arm and verified gameplay are still not enough.

        Reaches ACTIVE first, because the state gate is checked before the attestation -
        which is the correct order: input is locked outright until the countdown has
        completed, and only then is the attestation consulted.
        """
        machine, session, token = armed_machine(countdown_ms=0.0)
        foreground(session.hwnd, session.pid)
        time.sleep(0.002)
        # Arm with an empty attestation: the countdown itself must already refuse it.
        assert machine.confirm_active(ACTIVE_GAMEPLAY).allowed

        # Now revoke the confirmation while ACTIVE.
        token.user_attestation = ""
        result = machine.authorize("forward")
        assert result.blocked
        assert result.reason is BlockReason.GAMEPLAY_UNCONFIRMED, result.detail
        assert machine.state is ExecutionState.PAUSED_SAFE_STOP

    def test_5_arm_without_valid_capture_is_rejected(self, monkeypatch):
        machine = ExecutionStateMachine(run_id="r1")
        machine.observe()
        machine.select_target(FakeSession(), DEFAULT_EARLY_GAMEPLAY_PROFILE)
        # No capture was marked ready, so arming must be refused.
        token = ArmToken(session_id="sess-4242", profile_name="p",
                         user_attestation="I am in active gameplay.")
        result = machine.begin_countdown(token)
        assert result.blocked
        assert result.reason in (BlockReason.NOT_ARMED, BlockReason.CAPTURE_UNAVAILABLE)

    def test_6_countdown_never_emits_input(self):
        machine, session, _token = armed_machine(countdown_ms=30_000.0)
        assert machine.state is ExecutionState.ARMED_COUNTDOWN
        assert machine.authorize("forward").blocked
        assert machine.banner().startswith("INPUT ARMED")


# ------------------------------------------------------------------ 7..8


class TestCountdownCompletion:
    def test_7_countdown_ending_with_agent_foreground_denies_active(self, foreground):
        machine, _session, _token = armed_machine(countdown_ms=0.0)
        foreground(999, machine.self_pid)
        time.sleep(0.001)
        result = machine.confirm_active(ACTIVE_GAMEPLAY)
        assert result.blocked
        assert machine.state is ExecutionState.PAUSED_SAFE_STOP

    def test_8_countdown_ending_in_target_foreground_enters_active(self, foreground):
        machine, session, _token = armed_machine(countdown_ms=0.0)
        foreground(session.hwnd, session.pid)
        time.sleep(0.001)
        result = machine.confirm_active(ACTIVE_GAMEPLAY)
        assert not result.blocked, result.detail
        assert machine.state is ExecutionState.ACTIVE
        assert machine.status()["input_locked"] is False
        assert "INPUT ACTIVE" in machine.banner()


# ----------------------------------------------------------------- 9..13


class TestFailuresPauseAndRelease:
    def _active(self, foreground):
        machine, session, _token = armed_machine(countdown_ms=0.0)
        foreground(session.hwnd, session.pid)
        time.sleep(0.001)
        result = machine.confirm_active(ACTIVE_GAMEPLAY)
        assert not result.blocked, result.detail
        assert machine.state is ExecutionState.ACTIVE
        return machine, session

    def test_9_loss_of_game_foreground_pauses_safely(self, foreground):
        released = []
        machine, session = self._active(foreground)
        machine._release_hook = lambda: released.append(True)
        # An unrelated window takes the foreground. The target window itself is unchanged,
        # so this must be caught as a *focus* loss, not as an identity change.
        foreground(9001, 555001)
        result = machine.authorize("forward")
        assert result.blocked
        assert result.reason is BlockReason.TARGET_NOT_FOREGROUND, result.detail
        assert machine.state is ExecutionState.PAUSED_SAFE_STOP
        assert released, "held input was not released on foreground loss"

    def test_10_stale_capture_pauses_safely(self, foreground):
        machine, session = self._active(foreground)
        machine.last_capture = fresh_capture(session, age_ms=5000.0)
        result = machine.authorize("forward")
        assert result.blocked
        assert machine.state is ExecutionState.PAUSED_SAFE_STOP

    def test_11_gameplay_state_change_pauses_safely(self, foreground):
        machine, _session = self._active(foreground)
        # A rejected call pauses the machine, so each gameplay state gets a fresh one.
        for state in (GameplayState.MENU, GameplayState.PAUSED, GameplayState.LOADING,
                      GameplayState.UNKNOWN, GameplayState.CUTSCENE,
                      GameplayState.LIKELY_GAMEPLAY,
                      GameplayState.CAPTURE_UNAVAILABLE):
            machine, _session = self._active(foreground)
            machine.gameplay = GameplayVerdict(state, confidence=0.9)
            result = machine.authorize("forward")
            assert result.blocked, f"{state.value} permitted input"
            assert result.reason is BlockReason.GAMEPLAY_STATE_BLOCKED

    def test_12_emergency_stop_releases_held_key_and_stops_actions(self, foreground):
        released = []
        machine, _session = self._active(foreground)
        machine._release_hook = lambda: released.append(True)
        machine.emergency_stop("test")
        assert machine.state is ExecutionState.EMERGENCY_STOP
        assert released, "emergency stop did not release held input"
        assert machine.authorize("forward").blocked

    def test_13_target_identity_change_invalidates_the_session(self, foreground):
        machine, session = self._active(foreground)
        session.pid = 999999          # identity drift
        result = machine.authorize("forward")
        assert result.blocked
        assert result.reason in (BlockReason.TARGET_PROCESS_MISMATCH,
                                 BlockReason.TARGET_NOT_FOREGROUND)
        assert machine.state is ExecutionState.PAUSED_SAFE_STOP


# ----------------------------------------------------------------- 14..16


class TestProtectedAndProfile:
    def test_14_agent_foreground_denies_everything(self, foreground):
        machine, _session = TestFailuresPauseAndRelease()._active(foreground)
        foreground(1, machine.self_pid)
        result = machine.authorize("forward")
        assert result.blocked
        assert result.reason is BlockReason.SELF_FOREGROUND

    def test_15_forbidden_action_is_denied_by_the_profile(self, foreground):
        """Each intent is tested from a fresh ACTIVE machine.

        A rejected call pauses the machine by design, so testing several intents on one
        instance would measure the pause rather than the profile.
        """
        for intent in ("ctrl", "alt", "win", "menu", "apps", "jump"):
            machine, _session = TestFailuresPauseAndRelease()._active(foreground)
            result = machine.authorize(intent)
            assert result.blocked, f"{intent} was permitted"
            assert result.reason is BlockReason.PROFILE_FORBIDS_ACTION, (
                f"{intent} refused for the wrong reason: {result.reason}"
            )
        # The allowed one still works, on its own machine.
        machine, _session = TestFailuresPauseAndRelease()._active(foreground)
        assert machine.authorize("forward").allowed

    def test_16_completed_run_releases_and_stays_locked(self, foreground):
        released = []
        machine, _session = TestFailuresPauseAndRelease()._active(foreground)
        machine._release_hook = lambda: released.append(True)
        machine.complete()
        assert machine.state is ExecutionState.COMPLETED
        assert released
        assert machine.authorize("forward").blocked
        assert machine.status()["input_locked"] is True

    def test_system_intents_are_never_injectable_even_if_allowed(self):
        """A profile that permits 'win' must still be refused."""
        from frameforge.actions.arm import SYSTEM_INTENTS

        profile = InputProfile(name="bad", allowed_keys=frozenset(SYSTEM_INTENTS))
        for intent in SYSTEM_INTENTS:
            ok, why = profile.permits(intent)
            assert not ok, f"{intent} was permitted by a permissive profile"
            assert "never injectable" in why

    def test_input_permitting_states_is_exactly_active(self):
        from frameforge.actions.arm import INPUT_PERMITTING_STATES

        assert INPUT_PERMITTING_STATES == frozenset({ExecutionState.ACTIVE})


class TestVisionCannotAuthorise:
    """AI confidence is not authorization."""

    def test_proposal_has_no_injection_capability(self):
        proposal = VisionProposal(
            frame_id="f1", session_id="sess-4242",
            type=ProposalType.CLICK_RIGHT,
            point_in_target_client=SurfacePx(100, 200),
            region=Region(0.0, 0.0, 1.0, 1.0),
            confidence=0.99, visible=True,
        )
        # The type carries no way to express "inject".
        assert not hasattr(proposal, "send")
        assert not hasattr(proposal, "inject")
        assert not hasattr(proposal, "set_allowed_region")
        # And it is unauthorised until somebody says otherwise.
        assert proposal.authorized is False

    def test_stale_proposal_is_rejected(self):
        session = FakeSession()
        proposal = VisionProposal(
            session_id=session.session_id, type=ProposalType.CLICK_LEFT,
            point_in_target_client=SurfacePx(1, 1),
            confidence=0.95, visible=True, max_age_ms=1.0,
        )
        time.sleep(0.01)
        ok, why = verify_proposal(proposal, session)
        assert not ok and "old" in why

    def test_proposal_bound_to_another_session_is_rejected(self):
        session = FakeSession()
        proposal = VisionProposal(
            session_id="some-other-session", type=ProposalType.CLICK_LEFT,
            point_in_target_client=SurfacePx(1, 1), confidence=0.95, visible=True,
        )
        ok, why = verify_proposal(proposal, session)
        assert not ok and "different" not in why  # rejected, for the session reason
        assert not ok

    def test_low_confidence_proposal_is_rejected(self):
        session = FakeSession()
        proposal = VisionProposal(
            session_id=session.session_id, type=ProposalType.CLICK_LEFT,
            point_in_target_client=SurfacePx(1, 1), confidence=0.4, visible=True,
        )
        ok, why = verify_proposal(proposal, session)
        assert not ok and "confidence" in why

    def test_proposal_states_client_coordinates_not_screen_coordinates(self):
        session = FakeSession(client_origin=(1920, 0))
        proposal = VisionProposal(
            session_id=session.session_id, type=ProposalType.CLICK_LEFT,
            point_in_target_client=SurfacePx(100, 200),
            confidence=0.95, visible=True,
        )
        screen = to_screen(proposal, session)
        assert (screen.x, screen.y) == (2020, 200)
        # A proposal cannot express a screen point at all.
        with pytest.raises(Exception):
            to_screen(VisionProposal(session_id=session.session_id,
                                     type=ProposalType.CLICK_LEFT,
                                     confidence=0.95, visible=True), session)

    def test_region_is_evaluated_in_client_space(self):
        session = FakeSession(client_size=(1000, 800))
        region = Region(0.0, 0.0, 0.5, 0.5)
        assert region.contains(SurfacePx(100, 100), 1000, 800) is True
        assert region.contains(SurfacePx(900, 700), 1000, 800) is False


class TestCaptureFreshness:
    def test_fresh_capture_is_usable(self):
        frame = fresh_capture(FakeSession())
        assert frame.usable() == (True, "")

    def test_stale_capture_is_not_usable(self):
        frame = fresh_capture(FakeSession(), age_ms=900.0)
        ok, why = frame.usable()
        assert not ok and "old" in why

    def test_unhealthy_capture_is_not_usable(self):
        frame = fresh_capture(FakeSession(), healthy=False)
        ok, why = frame.usable()
        assert not ok and "black" in why

    def test_capture_is_bound_to_its_session(self):
        session = FakeSession()
        frame = fresh_capture(session)
        assert frame.target_hwnd == session.hwnd
        assert frame.target_pid == session.pid
        assert frame.session_id == session.session_id
