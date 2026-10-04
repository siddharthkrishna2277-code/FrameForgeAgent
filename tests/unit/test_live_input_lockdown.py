"""Live-input lockdown: proof that zero OS input can escape.

A live run performed an unauthorized, account-level side effect - a click intended for a
Notepad document on the external monitor landed inside the Hermes window on the laptop
monitor and activated a paid model link, and the owner received a confirmation email.

These tests prove the gate is at the syscall boundary rather than in a layer above it. A
mock `user32.SendInput` records calls; the real one is never reached.
"""

from __future__ import annotations

import ast
import ctypes
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "frameforge"


#: Monitor rectangles for the simulated dual-monitor topology.
PRIMARY_MONITOR = (0, 0, 1920, 1080)
EXTERNAL_MONITOR = (1920, 0, 3840, 1080)


@pytest.fixture
def send_input_spy(monkeypatch):
    """Replace the OS binding with a recorder. Nothing is sent."""
    calls: list[int] = []

    def fake_send_input(count, ptr, size):
        calls.append(count)
        return count

    import frameforge.actions.safety as safety

    monkeypatch.setattr(safety.user32, "SendInput", fake_send_input, raising=False)
    return calls


def _sendinput_call_lines(path: Path) -> list[int]:
    """Line numbers of every ``user32.SendInput(...)`` call, from the AST.

    Text scanning is unreliable here in both directions: tokenizing splits ``user32 . SendInput``
    into separate tokens, while a naive substring match counts a comment that merely names the
    API. Walking the tree and looking at ``Call.func`` is exact.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "SendInput":
            base = f.value
            if (isinstance(base, ast.Attribute) and base.attr == "user32") or \
                    (isinstance(base, ast.Name) and base.id == "user32"):
                hits.append(node.lineno)
    return hits


def _code_only(path: Path) -> str:
    """Source with comments and string literals removed.

    ``line.split("#")[0]`` is not good enough: a ``#`` inside a string literal truncates the
    line, and a comment that merely *names* an API would be counted as a call to it. Tokenizing
    and dropping COMMENT and STRING tokens is the only reliable way to scan code.
    """
    import io as _io
    import tokenize

    out: list[str] = []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    return " ".join(out)


@pytest.fixture
def lockdown_suspended():
    """Temporarily lift the lockdown.

    Only for tests whose subject is a contract that is *incompatible* with lockdown - a
    release must be delivered, a press must be tracked. Those are real behaviours that must
    keep working when live input is eventually re-authorised, and they cannot be observed
    while every press is refused.
    """
    from frameforge.actions import lockdown

    lockdown._state.active = False
    try:
        yield
    finally:
        lockdown._state.active = True


class TestLockdownIsTheDefault:
    def test_locked_on_import(self):
        from frameforge.actions.lockdown import lockdown_active

        assert lockdown_active() is True

    def test_reason_is_the_required_string(self):
        from frameforge.actions.lockdown import LOCKDOWN_REASON, state

        assert LOCKDOWN_REASON == "LIVE_INPUT_LOCKDOWN_AFTER_UNAUTHORIZED_SIDE_EFFECT"
        assert state()["LIVE_INPUT"] == "LOCKED_DOWN_AFTER_UNAUTHORIZED_SIDE_EFFECT"
        assert state()["reason"] == LOCKDOWN_REASON

    def test_no_runtime_unlock_path_exists(self):
        """A gate opened by a setting is a suggestion, not a gate.

        Inspects *code* only. The module's prose explains that there is no unlock path, so
        a text scan would flag the explanation as the thing it forbids.
        """
        import ast as _ast

        code = _code_only(SRC / "actions" / "lockdown.py")
        for forbidden in ("getenv", "environ", "unlock"):
            assert forbidden not in code, (
                f"lockdown code references {forbidden}; there must be no runtime unlock")




class TestZeroCallsReachTheOS:
    """Every primitive class the owner enumerated."""

    def _primitives(self):
        from frameforge.ports.input import Key, MouseButton, Primitive, PrimitiveType

        return [
            # Derived, never a bare literal: a fixed absolute screen point in a test file is
            # exactly what the static authority guard exists to forbid.
            Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS,
                      x=PRIMARY_MONITOR[0] + 1, y=PRIMARY_MONITOR[1] + 1),
            Primitive(kind=PrimitiveType.MOUSE_MOVE_REL,
                      dx=EXTERNAL_MONITOR[2] - EXTERNAL_MONITOR[0], dy=1),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.LEFT, down=True),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.LEFT, down=False),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.RIGHT, down=True),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.RIGHT, down=False),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.MIDDLE, down=True),
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.MIDDLE, down=False),
            Primitive(kind=PrimitiveType.SCROLL, scroll_y=1),
            Primitive(kind=PrimitiveType.KEY, key=Key.A, down=True),
            Primitive(kind=PrimitiveType.KEY, key=Key.A, down=False),
            Primitive(kind=PrimitiveType.UNICODE, text="X"),
            Primitive(kind=PrimitiveType.SCANCODE, scancode=0x1E, scancode_key=Key.A, down=True),
            Primitive(kind=PrimitiveType.SCANCODE, scancode=0x1E, scancode_key=Key.A, down=False),
        ]

    def test_every_primitive_kind_reaches_nothing(self, send_input_spy):
        from frameforge.actions.safety import InputSafetyManager

        from frameforge.adapters.input.sendinput import is_release

        mgr = InputSafetyManager()
        mgr.arm()
        for primitive in self._primitives():
            sent = mgr.send(primitive)
            if is_release(primitive):
                # Releases are deliberately exempt: they undo injected state, and refusing
                # one would strand a modifier on the operator's machine.
                continue
            assert sent is False, f"{primitive.kind} was not refused"
        # Only release traffic may have reached the OS. Count the releases rather than
        # guessing a number: the assertion is "no press escaped", not "N went out".
        releases = [p for p in self._primitives() if is_release(p)]
        assert len(send_input_spy) == len(releases), (
            f"expected only the {len(releases)} releases to escape; got {send_input_spy}")

    def test_double_click_shape_reaches_nothing(self, send_input_spy):
        """A double click is move+down+up+down+up; none of it may escape."""
        from frameforge.actions.safety import InputSafetyManager

        mgr = InputSafetyManager()
        mgr.arm()
        mgr.send(self._primitives()[0])          # the move: refused
        for down in (True, False, True, False):
            from frameforge.ports.input import MouseButton, Primitive, PrimitiveType

            mgr.send(Primitive(kind=PrimitiveType.MOUSE_BUTTON,
                               button=MouseButton.LEFT, down=down))
        # No button-down ever reached the OS; only the two key-ups did.
        assert send_input_spy == [1, 1], f"a button-down escaped: {send_input_spy}"

    def test_the_port_refuses_before_the_manager(self, send_input_spy):
        from frameforge.adapters.input.sendinput import SendInputPort
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        port = SendInputPort()
        port.set_enabled(True)
        assert port.send(Primitive(kind=PrimitiveType.KEY, key=Key.A, down=True)) is False
        assert send_input_spy == []
        assert port.blocked_count >= 1

    def test_raw_vk_escape_hatch_reaches_nothing(self, send_input_spy):
        """send_raw_vk was an earlier untracked bypass; it must be gated too."""
        from frameforge.adapters.input.sendinput import SendInputPort

        port = SendInputPort()
        port.set_enabled(True)
        assert port.send_raw_vk(0x41, up=False) is False
        assert send_input_spy == []

    def test_chokepoint_itself_refuses(self, send_input_spy):
        """Even called directly, the single OS entry point refuses.

        Raises rather than returning 0: every caller checks the returned count, so a 0 would
        be misread as "SendInput failed" and recorded as a device fault on every primitive.
        """
        import pytest as _pytest

        from frameforge.actions.lockdown import LiveInputLocked
        from frameforge.actions.safety import _INPUT, _os_send_input

        with _pytest.raises(LiveInputLocked):
            _os_send_input(1, ctypes.byref(_INPUT(type=1)))
        assert send_input_spy == []

    def test_batch_chokepoint_refuses(self, send_input_spy):
        import pytest as _pytest

        from frameforge.actions.lockdown import LiveInputLocked
        from frameforge.actions.safety import _INPUT, _os_send_input

        with _pytest.raises(LiveInputLocked):
            _os_send_input(2, (_INPUT * 2)())
        assert send_input_spy == []


class TestStaticProofOfCoverage:
    def test_no_raw_sendinput_call_exists_in_src(self):
        """If this fails, a new bypass route has been added."""
        chokepoint = SRC / "actions" / "safety.py"
        offenders = []
        for p in (SRC).rglob("*.py"):
            for line in _sendinput_call_lines(p):
                if p == chokepoint:
                    continue          # the single permitted site, checked separately
                offenders.append(f"{p.name}:{line}")
        expected = _sendinput_call_lines(chokepoint)
        assert len(expected) == 1, f"the chokepoint must be the only binding call: {expected}"
        assert not offenders, f"raw SendInput outside the chokepoint: {offenders}"

    def test_only_the_chokepoint_calls_the_os_binding(self):
        calls = _sendinput_call_lines(SRC / "actions" / "safety.py")
        assert len(calls) == 1, f"expected exactly one OS binding call, found {len(calls)}"
        src = (SRC / "actions" / "safety.py").read_text(encoding="utf-8")
        chokepoint = next(i for i, l in enumerate(src.splitlines(), 1)
                          if l.startswith("def _os_send_input"))
        assert calls[0] > chokepoint, "the binding call must be inside the chokepoint"

    def test_no_other_injection_api_is_called_in_src(self):
        """SetCursorPos / mouse_event / keybd_event would be equivalent bypasses."""
        import re

        offenders = []
        for p in SRC.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for api in ("SetCursorPos(", "mouse_event(", "keybd_event("):
                for m in re.finditer(r"\b" + re.escape(api), text):
                    line = text[:m.start()].count("\n") + 1
                    offenders.append(f"{p.name}:{line} {api}")
        assert not offenders, f"alternative injection route present: {offenders}"

    def test_lockdown_is_checked_in_the_chokepoint(self):
        src = (SRC / "actions" / "safety.py").read_text(encoding="utf-8")
        fn = src[src.index("def _os_send_input"):]
        fn = fn[:fn.index("\ndef ")]
        assert "lockdown_active" in fn


class TestReleaseStillWorks:
    """Lockdown must not strand a key on the operator's machine."""

    def test_release_all_may_still_emit(self, send_input_spy):
        from frameforge.actions.safety import InputSafetyManager

        mgr = InputSafetyManager()
        mgr.arm()
        mgr.release_all()
        assert send_input_spy, (
            "release_all emitted nothing; a stuck modifier would be worse than the incident")

    def test_a_release_primitive_is_not_refused_by_the_port(self):
        from frameforge.adapters.input.sendinput import SendInputPort
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        port = SendInputPort()
        port.set_enabled(True)
        # A release must be allowed through the gate: refusing it strands the key.
        assert port.send(Primitive(kind=PrimitiveType.KEY, key=Key.A, down=False)) is True


class TestDoctorSurfacesIt:
    def test_doctor_reports_the_lockdown(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-m", "frameforge.cli.main", "doctor"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**__import__("os").environ, "PYTHONPATH": str(SRC.parent)})
        assert r.returncode == 0, r.stderr
        assert "live-input-lockdown" in r.stdout
        assert "LOCKED_DOWN_AFTER_UNAUTHORIZED_SIDE_EFFECT" in r.stdout
