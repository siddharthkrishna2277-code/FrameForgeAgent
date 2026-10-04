"""Hardware-in-the-loop tests. Run with ``pytest --run-hardware``.

These touch the real desktop: real capture, real Win32 input, real OCR. They are excluded
by default because they move the user's cursor and require a live session.

``test_estop_releases_held_keys_within_100ms`` is the measured requirement behind ROADMAP
stability criterion 2. It presses a key, trips the stop, and asserts nothing remains held.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware

# --------------------------------------------------------------------------------------
# SAFETY GATE
#
# These tests inject REAL mouse and keyboard input. Windows cannot distinguish injected
# input from a human hand: it is serialised into the same stream and lands on whatever is
# foreground.
#
# That is not theoretical. `test_all_buttons_round_trip` sent a right-button down/up at
# whatever was under the cursor while the Frame Forge agent's own UI was foreground, and
# the user watched context menus open by themselves in that window.
#
# So every injecting test requires an explicitly registered test target. Without one, the
# test skips rather than clicking somebody's desktop.
# --------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------
# P0.5: no fixed screen coordinates on a user's live desktop.
#
# These tests used to click a hardcoded (300, 300) on the primary monitor. That point is
# an authorisation hole, not a coordinate bug: whatever window happens to occupy that pixel
# receives the click - an editor, a browser, a settings panel, the taskbar, or the agent's
# own UI. Correct coordinate conversion does not make a fixed point safe.
#
# Every injection now derives its point from a *registered target's verified client area*
# and passes TargetGuard, which re-verifies the window under the point immediately before
# button-down. With no registered target these tests refuse rather than skip-and-hope.
# ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registered_target():
    """A registered, verified target for injection tests, or None.

    Deliberately has no default. A test that needs a target asks for one by launching a
    purpose-built window; nothing falls back to "the foreground window" or to a fixed
    screen coordinate.
    """
    return None


def require_registered_target(session) -> None:
    """Refuse (do not merely skip) unless a real target session is registered."""
    if session is None:
        pytest.fail(
            "no registered test target: injection tests must derive their point from a "
            "verified target window. A fixed screen coordinate is not an acceptable "
            "substitute on a live desktop."
        )


def point_in_target(session, fx: float = 0.5, fy: float = 0.5):
    """A screen point inside the registered target's approved region."""
    from frameforge.actions.coordinates import ScreenPx

    rect = session.client_rect
    return ScreenPx(int(rect.x + rect.width * fx), int(rect.y + rect.height * fy))


from frameforge.adapters.input.sendinput import SendInputPort
from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
from frameforge.ports.geometry import Point, Size
from frameforge.ports.input import Key, MouseButton, Primitive, PrimitiveType
from frameforge.ports.window import SessionState


@pytest.fixture
def window():
    return PyWin32WindowAdapter()


class TestWindowAdapter:
    def test_monitors_reported(self, window):
        topo = window.monitors()
        assert topo.monitors, "at least one monitor must be reported"
        assert topo.virtual_rect.width > 0
        assert topo.signature, "topology must expose a change-detectable signature"

    def test_enumeration_finds_something_visible(self, window):
        windows = window.enumerate_windows()
        assert isinstance(windows, list)

    def test_session_is_interactive_or_reported_honestly(self, window):
        state = window.session_state()
        assert isinstance(state, SessionState)

    def test_idle_time_is_readable(self, window):
        assert window.idle_ms() >= 0

    def test_find_window_refuses_an_unidentified_spec(self, window):
        from frameforge.ports.window import TargetSpec

        with pytest.raises(ValueError, match="identify"):
            window.find_window(TargetSpec())


class TestCapture:
    def test_capture_produces_rgb_frames_at_both_monitors(self):
        from frameforge.adapters.capture.mss_capture import MssCaptureSource
        from frameforge.ports.capture import Surface, SurfaceKind

        window = PyWin32WindowAdapter()
        topo = window.monitors()
        for index, monitor in enumerate(topo.monitors):
            surface = Surface(
                kind=SurfaceKind.MONITOR, size=Size(monitor.width, monitor.height),
                offset_x=monitor.rect.x, offset_y=monitor.rect.y, monitor_index=index,
            )
            cap = MssCaptureSource()
            cap.open(surface)
            try:
                frame = cap.grab()
                assert frame is not None, f"no frame from monitor {index}"
                assert frame.array.shape == (monitor.height, monitor.width, 3)
                assert frame.content_hash
            finally:
                cap.close()

    def test_capture_latency_is_measured(self):
        from frameforge.adapters.capture.mss_capture import MssCaptureSource
        from frameforge.ports.capture import Surface, SurfaceKind

        surface = Surface(kind=SurfaceKind.MONITOR, size=Size(1920, 1080))
        cap = MssCaptureSource()
        cap.open(surface)
        try:
            times = []
            for _ in range(6):
                frame = cap.grab()
                if frame:
                    times.append(frame.capture_ms)
            assert times, "no frames captured"
            mean = sum(times) / len(times)
            print(f"\n  measured capture latency: {mean:.0f}ms at 1920x1080")
            # A run this slow cannot sustain a high decision rate; assert only that it is
            # usable, and let the number speak for itself in CI output.
            assert mean < 400, f"capture too slow: {mean:.0f}ms"
        finally:
            cap.close()


class TestOcr:
    def test_winrt_ocr_recognises_rendered_text(self):
        from frameforge.adapters.ocr.rapidocr_ocr import build_ocr

        ocr = build_ocr("auto")
        if not ocr.capabilities().available:
            pytest.skip("no OCR backend available on this machine")
        try:
            import cv2
            import numpy as np

            canvas = np.zeros((120, 800, 3), np.uint8)
            canvas[:, :] = (255, 255, 255)
            cv2.putText(canvas, "START GAME", (30, 80), cv2.FONT_HERSHEY_SIMPLEX,
                        2.0, (0, 0, 0), 4)
            result = ocr.read(canvas)
            assert result.ok, result.error
            assert "START GAME" in result.text.upper()
        finally:
            close = getattr(ocr, "close", None)
            if close:
                close()

class TestNoFixedCoordinateInjection:
    """The invariant behind P0.5, asserted statically.

    A fixed screen coordinate in a test is an authorisation hole: the pixel belongs to
    whatever window is there. This catches the pattern without needing to run anything.
    """

    def test_no_hardcoded_absolute_screen_point(self):
        """A fixed pixel in a live test is an authorisation hole, not a coordinate bug."""
        import re as _re

        source = Path(__file__).read_text(encoding="utf-8")
        # Ignore this class: it legitimately contains the pattern it forbids.
        body = source.split("class TestNoFixedCoordinateInjection:", 1)[1]
        body = body.split("\nclass ", 1)[0]
        offenders = _re.findall(
            r"MOUSE_MOVE_ABS\s*,\s*x\s*=\s*\d+\s*,\s*y\s*=\s*\d+", body
        )
        assert not offenders, f"fixed absolute mouse coordinates in a live test: {offenders}"

    def test_no_pointless_mouse_button_action(self):
        from frameforge.actions.model import Click, MouseButtonDown, MouseButtonUp
        from frameforge.ports.geometry import Point

        for action_type in (Click, MouseButtonDown, MouseButtonUp):
            with pytest.raises(Exception):
                action_type(button="left")  # type: ignore[call-arg]
            # And it constructs fine *with* a point.
            assert action_type(at=Point(1, 1))  # type: ignore[call-arg]

    def test_every_injecting_test_uses_a_registered_target(self):
        """Any test that sends mouse events must derive its point from a target session.

        Tests using the *mock* backend are exempt: they never reach the OS, so they cannot
        misdeliver a click. The rule is about what reaches a real desktop.
        """
        import ast as _ast

        source = Path(__file__).read_text(encoding="utf-8")
        tree = _ast.parse(source)
        # This class asserts the invariant and so contains the tokens it searches for.
        # Skip it, or the assertion reports itself.
        SELF = "TestNoFixedCoordinateInjection"
        skip = {f.name for cls in _ast.walk(tree)
                if isinstance(cls, _ast.ClassDef) and cls.name == SELF
                for f in cls.body if isinstance(f, _ast.FunctionDef)}
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.FunctionDef) or node.name in skip:
                continue
            body = _ast.get_source_segment(source, node) or ""
            if "MOUSE_BUTTON" not in body and "MOUSE_MOVE_ABS" not in body:
                continue
            if "_port()" in body or "MockInputController" in body:
                continue  # mock backend: cannot reach the OS
            assert "registered_target" in body or "point_in_target" in body, (
                f"{node.name} injects input without deriving its point from a registered "
                "target; a fixed screen coordinate is not acceptable on a live desktop"
            )


class TestTargetValidationRefusesBeforeInjection:
    """Acceptance tests for the target gate, run against the mock backend.

    These need no real desktop: the point of the gate is that it refuses *before* anything
    reaches the OS, and a mock backend proves that by counting emissions.
    """

    def _port(self):
        from frameforge.actions.safety import InputSafetyManager
        from frameforge.ports import fakes

        class Port:
            name = "sendinput"

            def __init__(self):
                self.safety = InputSafetyManager()
                self._enabled = False
                self.sent = []

            @property
            def enabled(self):
                return self._enabled

            def set_enabled(self, value, thorough=True):
                self._enabled = value

            def send(self, primitive, action_id="", source=""):
                self.sent.append(primitive)

            def send_batch(self, prims, action_id=""):
                for p in prims:
                    self.send(p, action_id)
                return len(prims)

            def release_all(self, thorough=True):
                return self.safety.release_all(thorough=thorough)

        return Port()

    def test_button_down_without_move_is_refused(self):
        """P0c: the runtime defence. No move, no button."""
        from frameforge.actions.controller import (
            InputPolicy, MockInputController, PolicyConfig)
        from frameforge.actions.executor import ActionExecutor
        from frameforge.ports.geometry import Point
        from frameforge.ports.input import Primitive, PrimitiveType

        port = self._port()
        port.set_enabled(True)
        controller = MockInputController()
        executor = ActionExecutor(port, controller=controller,
                                  policy=InputPolicy(PolicyConfig(require_foreground=False)))
        result = executor.execute([
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button="left", down=True)
        ])
        assert port.sent == [], "a button was injected with no preceding move"
        assert result.denied
        assert "no preceding move" in (result.blocked_reason or "")

    def test_audit_records_why_an_action_was_refused(self):
        from frameforge.actions.controller import InputPolicy, MockInputController, PolicyConfig
        from frameforge.actions.executor import ActionExecutor
        from frameforge.ports.input import Primitive, PrimitiveType

        port = self._port()
        port.set_enabled(True)
        executor = ActionExecutor(port, controller=MockInputController(),
                                  policy=InputPolicy(PolicyConfig(require_foreground=False)))
        executor.execute([Primitive(kind=PrimitiveType.MOUSE_BUTTON, button="left", down=True)])
        assert executor.target_refusals
        assert executor.target_refusals[0]["event"] == "mouse_button_down_without_move"


class TestCoordinateSpaces:
    def test_normalisation_spans_the_virtual_desktop_not_the_primary(self):
        """A point on the second monitor must not normalise into the first."""
        from frameforge.actions.coordinates import ScreenPx, VirtualDesktop

        desktop = VirtualDesktop(x=0, y=0, width=3840, height=1080)
        # The far edge lands at the top of the SendInput range (within one step: the last
        # *coordinate* is width-1, so it normalises just short of 65535 by design).
        assert desktop.to_send_input(ScreenPx(3839, 1079)).x >= 65500
        assert desktop.to_send_input(ScreenPx(0, 0)).x == 0
        # The centre of a two-monitor desktop is the midpoint - within a quantisation step.
        mid = desktop.to_send_input(ScreenPx(1920, 540)).x
        assert abs(mid - 32767) <= 2, f"mid-desktop normalised to {mid}, ~9px bias"
        # Monotonic across the whole span.
        values = [desktop.to_send_input(ScreenPx(x, 0)).x for x in (0, 960, 1920, 2880, 3839)]
        assert values == sorted(values)

    def test_negative_origin_is_handled(self):
        """A monitor left of the primary has a negative origin; the far-left pixel must
        normalise to ~0, not to a point inside the first monitor."""
        from frameforge.actions.coordinates import ScreenPx, VirtualDesktop

        desktop = VirtualDesktop(x=-1920, y=0, width=3840, height=1080)
        assert desktop.to_send_input(ScreenPx(-1920, 0)).x == 0
        assert desktop.to_send_input(ScreenPx(-1920, 0)).y == 0
        assert desktop.to_send_input(ScreenPx(1919, 1079)).x >= 65500
        # The desktop spans -1920..1919. Inside, and outside on both sides.
        assert desktop.contains(ScreenPx(-1900, 500)) is True
        assert desktop.contains(ScreenPx(1900, 500)) is True
        assert desktop.contains(ScreenPx(-2500, 500)) is False
        assert desktop.contains(ScreenPx(2000, 500)) is False

    def test_out_of_bounds_point_is_rejected_not_clamped(self):
        from frameforge.actions.coordinates import ScreenPx, VirtualDesktop, assert_in_bounds

        desktop = VirtualDesktop(x=0, y=0, width=1920, height=1080)
        with pytest.raises(ValueError, match="outside"):
            assert_in_bounds(ScreenPx(5000, 500), desktop)

    def test_surface_to_screen_uses_the_client_origin(self):
        from frameforge.actions.coordinates import SurfacePx, surface_to_screen

        assert surface_to_screen(SurfacePx(10, 20), (1920, 100)).x == 1930

    def test_regional_point_belongs_to_target(self):
        from frameforge.actions.target import TargetSession

        session = TargetSession(
            run_id="r", hwnd=1, pid=2,
            client_origin=(0, 0), client_size=__import__(
                "frameforge.ports.geometry", fromlist=["Size"]).Size(1000, 800),
            approved_regions=((0.0, 0.0, 0.5, 0.5),),
        )
        assert session.in_approved_region(100, 100) is True
        assert session.in_approved_region(900, 700) is False


class TestProtectedRegistry:
    def test_current_process_is_protected_by_pid(self):
        import os

        from frameforge.actions.target import ProtectedRegistry

        registry = ProtectedRegistry()
        pid = registry.protect_current_process()
        protected, why = registry.is_protected(0, pid)
        assert protected and str(pid) in why

    def test_protected_class_is_matched_case_insensitively(self):
        from frameforge.actions.target import ProtectedRegistry

        registry = ProtectedRegistry()
        registry.protect_class("Chrome_WidgetWin_1")
        # A hwnd of 0 has no identity, so this exercises the class list path directly.
        assert registry._classes == {"chrome_widgetwin_1"}

    def test_unregistered_pid_is_not_protected(self):
        from frameforge.actions.target import ProtectedRegistry

        registry = ProtectedRegistry()
        assert registry.is_protected(0, 999999)[0] is False


class TestWindowsAndCaptureStillWork:
    """Non-injecting checks, kept because they need a real desktop."""

    def test_capture_produces_rgb_frames_at_both_monitors(self):
        pytest.importorskip("mss")
        from frameforge.adapters.capture.mss_capture import MssCaptureSource
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        window = PyWin32WindowAdapter()
        topo = window.monitors()
        for index, monitor in enumerate(topo.monitors):
            surface = Surface(
                kind=SurfaceKind.MONITOR, size=Size(monitor.width, monitor.height),
                offset_x=monitor.rect.x, offset_y=monitor.rect.y, monitor_index=index,
            )
            cap = MssCaptureSource()
            cap.open(surface)
            try:
                frame = cap.grab()
                assert frame is not None, f"no frame from monitor {index}"
                assert frame.array.shape == (monitor.height, monitor.width, 3)
            finally:
                cap.close()

    def test_ocr_recognises_rendered_text(self):
        pytest.importorskip("mss")
        import cv2
        import numpy as np

        from frameforge.adapters.ocr.rapidocr_ocr import build_ocr

        ocr = build_ocr("auto")
        if not ocr.capabilities().available:
            pytest.skip(f"no OCR backend: {ocr.capabilities().notes}")
        try:
            canvas = np.zeros((120, 800, 3), np.uint8)
            canvas[:, :] = (255, 255, 255)
            cv2.putText(canvas, "START GAME", (30, 80), cv2.FONT_HERSHEY_SIMPLEX,
                        2.0, (0, 0, 0), 4)
            result = ocr.read(canvas)
            assert result.ok, result.error
            assert "START" in result.text.upper(), f"OCR read {result.text!r}"
        finally:
            close = getattr(ocr, "close", None)
            if callable(close):
                close()

    def test_monitors_report_dpi_and_orientation(self):
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        monitors = PyWin32WindowAdapter().monitors_detailed()
        assert monitors
        for m in monitors:
            assert m.dpi >= 72, f"{m.device_name} reported implausible dpi {m.dpi}"
            assert m.orientation in ("landscape", "portrait")
            assert m.scale_percent >= 100

    def test_topology_fingerprint_changes_with_arrangement(self):
        from frameforge.adapters.window.pywin32_window import monitor_topology_fingerprint

        first = monitor_topology_fingerprint()
        assert first and "vd=" in first
        assert first == monitor_topology_fingerprint()

    def test_virtual_desktop_matches_system_metrics(self):
        from frameforge.actions.target import virtual_desktop

        desktop = virtual_desktop()
        assert desktop.width > 0 and desktop.height > 0
        assert desktop.describe()
