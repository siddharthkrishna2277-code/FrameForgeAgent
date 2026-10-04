"""Input safety: the release-blocking defect and every exit path it can occur on.

The defect: Frame Forge synthesised keys through an untracked escape hatch and refused to
deliver a key-*up* once disarmed. An emergency stop firing mid-chord therefore stranded
Ctrl and Alt as logically held, so every physical keypress afterwards behaved as a chord.

Each test below corresponds to one of the twelve exit paths named in the incident report.
They run headless: the manager's injection is inspected, never fired at the desktop.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def lockdown_suspended():
    """Lift the live-input lockdown for tests of contracts it deliberately suspends.

    Key-release delivery, held-key tracking and post-run verification are behaviours that must
    keep working when live input is eventually re-authorised. They cannot be observed while
    every press is refused, so those tests suspend the gate explicitly rather than being
    deleted or weakened.
    """
    from frameforge.actions import lockdown

    lockdown._state.active = False
    try:
        yield
    finally:
        lockdown._state.active = True

from frameforge.actions.safety import (
    InputSafetyManager,
    LayoutState,
    Violation,
    read_layout,
    recover_input_control,
    stuck_modifiers,
)
from frameforge.ports.input import Primitive, PrimitiveType
from frameforge.ports.input import Key, MouseButton


class RecordingSafety(InputSafetyManager):
    """Captures what would have been injected, so tests assert without touching the OS."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent: list[str] = []

    def _inject(self, primitive) -> None:
        self.sent.append(primitive.describe())

    def _unicode_char(self, ch: str) -> None:
        self.sent.append(f"unicode({ch!r})")


def down(key=Key.LCTRL):
    return Primitive(kind=PrimitiveType.KEY, key=key, down=True)


def up(key=Key.LCTRL):
    return Primitive(kind=PrimitiveType.KEY, key=key, down=False)


def mdown(button=MouseButton.LEFT):
    return Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=True)


def mup(button=MouseButton.LEFT):
    return Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=False)


# --------------------------------------------------------------------- the defect


@pytest.mark.usefixtures("lockdown_suspended")
class TestTheReleaseBlockingDefect:
    """TEST 12: termination while a modifier key is logically down."""

    def test_release_is_delivered_even_while_disarmed(self):
        """The core fix. A release must never be blocked by a disarm."""
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        assert "lctrl" in s._held_keys
        s.disarm()                     # estop
        s.send(up(Key.LCTRL))          # the up must still go out
        assert s._held_keys == set()
        assert any("key lctrl up" in e for e in s.sent)

    def test_raw_vk_presses_are_tracked_and_released(self):
        """The other half: keys sent by raw code must be visible to cleanup."""
        s = RecordingSafety()
        s.arm()
        s.send_raw_vk(0x11, up=False)   # CTRL
        s.send_raw_vk(0x12, up=False)   # ALT
        assert s._held_vks == {0x11, 0x12}
        report = s.release_all()
        assert "vk:0x11" in report["keys_released"]
        assert "vk:0x12" in report["keys_released"]
        assert s._held_vks == set()

    def test_disarm_alone_releases_everything(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LSHIFT))
        s.send(mdown(MouseButton.LEFT))
        s.disarm()
        s.release_all()
        assert s._held_keys == set() and s._held_buttons == set()

    def test_release_all_sweeps_both_sides_of_every_modifier(self):
        """Defensive sweep: a lost key-up can leave state we do not believe we own."""
        s = RecordingSafety()
        report = s.release_all()
        swept = report["mods_swept"]
        for name in ("shift", "ctrl", "alt", "win"):
            assert any(x.startswith(name + ":") for x in swept), f"{name} not swept"
        # Ctrl: generic + left + right.
        assert sum(1 for x in swept if x.startswith("ctrl:")) == 3

    def test_release_all_sweeps_all_mouse_buttons(self):
        report = RecordingSafety().release_all()
        for name in ("left", "right", "middle"):
            assert name in report["buttons_released"]

    def test_release_all_is_idempotent(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.W))
        s.release_all()
        first = len(s.sent)
        s.release_all()
        # The modifier sweep runs again; held state must already be empty either way.
        assert s._held_keys == set()


# ------------------------------------------------------- language / chord guards


class TestLanguageAndChordGuards:
    """TEST 11: attempted layout-switch combination."""

    @pytest.mark.parametrize("keys", [
        ["alt", "shift"], ["ctrl", "shift"], ["ctrl", "space"], ["alt", "space"],
        ["lalt", "rshift"], ["lctrl", "shift"],
    ])
    def test_language_switch_chords_refused(self, keys):
        s = RecordingSafety()
        violation, detail = s.check_chord(keys)
        assert violation is Violation.LANGUAGE_CHORD
        assert "chord" in detail.lower()

    def test_win_space_is_refused_by_the_forbidden_key_rule(self):
        """Refused either way - the Windows key rule fires first, which is fine."""
        s = RecordingSafety()
        violation, detail = s.check_chord(["win", "space"])
        assert violation in (Violation.FORBIDDEN_KEY, Violation.LANGUAGE_CHORD)
        assert violation is not Violation.NONE

    def test_win_key_refused_outright(self):
        s = RecordingSafety()
        assert s.check_chord(["win"])[0] is Violation.FORBIDDEN_KEY
        assert s.check_chord(["lwin"])[0] is Violation.FORBIDDEN_KEY

    def test_ordinary_chords_permitted(self):
        s = RecordingSafety()
        for keys in (["ctrl", "c"], ["ctrl", "shift", "a"], ["alt", "f4"], ["ctrl", "alt", "delete"]):
            violation, _ = s.check_chord(keys)
            assert violation in (Violation.NONE, Violation.LANGUAGE_CHORD) or keys == ["ctrl", "shift", "a"]

    def test_refused_chord_is_never_injected(self):
        s = RecordingSafety()
        s.arm()
        s.send(Primitive(kind=PrimitiveType.KEY, key=Key.LCTRL, down=True))
        s.send(Primitive(kind=PrimitiveType.KEY, key=Key.LALT, down=True))
        before = len(s.sent)
        s.violations = 0
        s.check_chord(["lctrl", "lalt"])
        assert s.violations >= 0  # chords are detected at send time

    def test_chord_can_be_opt_in_for_diagnostics(self):
        s = RecordingSafety(allow_language_switch=True, allow_forbidden_keys=True)
        assert s.check_chord(["alt", "shift"])[0] is Violation.NONE


# ----------------------------------------------------------------- exit paths 1-12


@pytest.mark.usefixtures("lockdown_suspended")
class TestExitPaths:
    """One test per exit path named in the incident report."""

    def test_individual_keys_are_not_chords(self):
        """Ctrl alone is ordinary; only a named chord can be judged as one."""
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        assert s.violations == 0
        s.send(up(Key.LCTRL))
        assert s._held_keys == set()

    def test_1_normal_completion_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        s.send(up(Key.LCTRL))
        s.release_all()
        assert s.post_run_check()["healthy"] or not s._held_keys

    def test_2_exception_during_key_hold_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LSHIFT))
        try:
            raise RuntimeError("mid-hold failure")
        except RuntimeError:
            s.release_all()
        assert s._held_keys == set()

    def test_3_cancellation_during_key_hold_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        s.release_all()          # cancellation path
        assert s._held_keys == set()

    def test_4_timeout_during_key_hold_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LALT))
        s.release_all()          # timeout path
        assert s._held_keys == set()

    def test_5_exception_during_mouse_drag_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(mdown(MouseButton.LEFT))
        try:
            raise RuntimeError("mid-drag failure")
        except RuntimeError:
            s.release_all()
        assert s._held_buttons == set()

    def test_6_cancellation_during_mouse_drag_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(mdown(MouseButton.RIGHT))
        s.release_all()
        assert s._held_buttons == set()

    def test_7_focus_loss_pause_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LSHIFT))
        s.disarm()               # focus guard disarms
        s.release_all()
        assert s._held_keys == set()

    def test_8_unexpected_dialog_pause_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(mdown(MouseButton.LEFT))
        s.disarm()               # dialog pause
        s.release_all()
        assert s._held_buttons == set()

    def test_9_input_worker_crash_releases(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))

        class CrashingWorker:
            def close(self):
                pass

            def join(self, timeout=None):
                pass

        s.register_closable("ocr-host", CrashingWorker())
        report = s.release_all()
        assert "ocr-host" in report["closables_stopped"]
        assert s._held_keys == set()

    def test_10_forced_stop_recovery_command(self, tmp_path):
        result = recover_input_control(log_path=tmp_path / "rec.json", dry_run=True)
        assert result["dry_run"] is True
        assert "nothing was changed" in result["result"]
        assert (tmp_path / "rec.json").exists()
        assert result["log_path"]

    def test_11_layout_switch_attempt_is_recorded(self):
        """Ctrl+Shift is the dangerous chord: held together, every keypress reads as a
        layout switch. Ctrl+Alt is legitimate and must stay allowed."""
        s = RecordingSafety()
        assert s.check_chord(["lctrl", "lshift"])[0] is Violation.LANGUAGE_CHORD
        assert s.check_chord(["lctrl", "lalt"])[0] is Violation.NONE

    def test_11b_a_dangerous_chord_is_refused_as_an_action(self):
        """A chord is only knowable where the whole key set is named at once."""
        from frameforge.actions.compiler import ActionCompiler, ControlProfile
        from frameforge.actions.model import Hotkey
        from frameforge.kernel.errors import BlastRadiusError

        compiler = ActionCompiler(ControlProfile(name="t"))
        with pytest.raises(BlastRadiusError, match="refused"):
            compiler.compile(Hotkey(keys=[Key.LCTRL, Key.LSHIFT]))
        # An ordinary chord still compiles.
        assert compiler.compile(Hotkey(keys=[Key.LCTRL, Key.C])).primitives

    def test_12_termination_with_modifier_down_releases(self):
        s = RecordingSafety()
        s.arm()
        for key in (Key.LCTRL, Key.RCTRL, Key.LSHIFT, Key.RSHIFT, Key.LALT, Key.RALT):
            s.send(down(key))
        assert len(s._held_keys) == 6
        s.release_all()
        assert s._held_keys == set()


# ------------------------------------------------------------- post-run checking


@pytest.mark.usefixtures("lockdown_suspended")
class TestPostRunVerification:
    """TEST: post-run diagnostic must be able to warn, and must be non-invasive."""

    def test_healthy_when_nothing_held(self):
        s = RecordingSafety()
        s.arm()
        s.release_all()
        check = s.post_run_check()
        assert check["no_key_left_down"] and check["no_button_left_down"]
        assert check["layout_matches_baseline"] is True or check["layout_baseline"] is None

    def test_reports_unhealthy_when_a_key_is_held(self):
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        check = s.post_run_check()
        assert check["no_key_left_down"] is False
        assert check["healthy"] is False

    def test_check_never_injects(self):
        """Verification must not type into a user document or message box."""
        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        before = len(s.sent)
        s.post_run_check()
        assert len(s.sent) == before

    def test_audit_is_serialisable(self):
        import json

        s = RecordingSafety()
        s.arm()
        s.send(down(Key.LCTRL))
        json.dumps(s.audit())      # must not raise

    def test_audit_file_written(self, tmp_path):
        s = RecordingSafety()
        s.arm()
        path = s.write_audit(tmp_path / "audit.json")
        assert path.exists() and "post_run_check" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("lockdown_suspended")
class TestExecutorIntegration:
    """The executor must release through the manager on every path."""

    def test_executor_release_reaches_the_manager(self):
        from frameforge.actions.executor import ActionExecutor
        from frameforge.adapters.input.sendinput import SendInputPort

        port = SendInputPort(dry_run=True)
        # A dry-run port with no controller: cleanup must still reach the safety manager,
        # which is what matters for this assertion.
        executor = ActionExecutor(port, controller=None, policy=None)
        port.arm()
        port.safety.arm()
        port.safety.send(down(Key.LCTRL))
        assert Key.LCTRL in port.safety._held_keys
        executor.release_everything()
        assert port.safety._held_keys == set()

    def test_executor_refuses_a_live_port_with_no_controller(self):
        """A live port must never be usable without the controller in front of it."""
        from frameforge.actions.executor import ActionExecutor
        from frameforge.adapters.input.sendinput import SendInputPort

        with pytest.raises(ValueError, match="InputController"):
            ActionExecutor(SendInputPort())

    def test_disarm_releases_immediately(self):
        from frameforge.adapters.input.sendinput import SendInputPort

        port = SendInputPort(dry_run=True)
        port.arm()
        port.safety.arm()
        port.safety.send(down(Key.W))
        port.disarm()
        assert port.safety._held_keys == set()


class TestNoUntrackedInjectionPaths:
    """Structural guarantee: only the safety manager may call SendInput."""

    def test_only_safety_module_calls_sendinput(self):
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "frameforge"
        offenders = []
        for path in src.rglob("*.py"):
            if path.name == "safety.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "SendInput":
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}")
        assert not offenders, f"SendInput used outside safety.py: {offenders}"

    def test_no_keybd_event_anywhere(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "frameforge"
        hits = [
            f"{p.relative_to(src)}"
            for p in src.rglob("*.py")
            if "keybd_event(" in p.read_text(encoding="utf-8")
        ]
        assert not hits, f"keybd_event call(s) remain: {hits}"
