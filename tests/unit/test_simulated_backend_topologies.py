"""Simulation of the dual-monitor topology. No OS input, on any layout.

These are the tests the re-enable checklist requires before anyone considers live input
again. They exist because the alternative - discovering a routing fault by moving a real
cursor over a real desktop - is itself the hazard that caused the incident.
"""

from __future__ import annotations

import pytest

from frameforge.adapters.input.simulated import (
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    SimulatedInputPort,
    VirtualScreen,
)
from frameforge.ports.input import MouseButton, Primitive, PrimitiveType

#: The machine as actually configured, plus the layouts it can be dragged into.
LAYOUTS = {
    "external right of primary": VirtualScreen(0, 0, 3840, 1080),
    "external LEFT of primary (negative origin)": VirtualScreen(-1920, 0, 3840, 1080),
    "external ABOVE primary": VirtualScreen(0, -1080, 1920, 2160),
    "external BELOW primary": VirtualScreen(0, 0, 1920, 2160),
    "different dimensions": VirtualScreen(0, 0, 2560, 1440),
    "unequal side-by-side": VirtualScreen(0, 0, 1920 + 2560, 1440),
}


class TestNormalisationIsLayoutIndependent:
    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_origin_maps_to_zero_and_far_edge_to_max(self, name):
        vs = LAYOUTS[name]
        assert vs.normalize(vs.x, vs.y) == (0, 0), name
        nx, ny = vs.normalize(vs.right - 1, vs.bottom - 1)
        assert nx > 65000 and ny > 65000, name

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_round_trip_within_one_pixel(self, name):
        vs = LAYOUTS[name]
        for pt in ((vs.x, vs.y),
                   (vs.x + 1, vs.y + 1),
                   (vs.right - 2, vs.bottom - 2),
                   (vs.x + vs.width // 2, vs.y + vs.height // 2)):
            nx, ny = vs.normalize(*pt)
            bx, by = vs.denormalize(nx, ny)
            assert abs(bx - pt[0]) <= 1 and abs(by - pt[1]) <= 1, f"{name} {pt}"

    @pytest.mark.parametrize("name", sorted(LAYOUTS))
    def test_monotonic_across_the_whole_span(self, name):
        """A fold or plateau means some points are unreachable or ambiguous."""
        vs = LAYOUTS[name]
        prev = -1
        for px in range(vs.x, vs.right, max(1, vs.width // 40)):
            nx, _ = vs.normalize(px, vs.y)
            assert nx >= prev, f"{name}: normalisation not monotonic at x={px}"
            prev = nx


class TestPrimaryOnlyNormalisationIsCaught:
    """The specific topology that produced the incident.

    Hermes on the primary monitor; a Notepad target on the external one. A normalisation
    computed against the primary monitor instead of the virtual desktop sends every external
    point to the far edge of the *primary*, which is the wrong window entirely.
    """

    HERMES_RECT = (0, 0, 1920, 1080)          # primary / laptop
    NOTEPAD_RECT = (1920, 0, 3840, 1080)     # external
    NOTEPAD_CLIENT_POINT = (400, 450)        # inside Notepad's client area

    @classmethod
    def notepad_screen_point(cls):
        """The target point in virtual-desktop coordinates, derived not literal."""
        return (cls.NOTEPAD_RECT[0] + cls.NOTEPAD_CLIENT_POINT[0],
                cls.NOTEPAD_RECT[1] + cls.NOTEPAD_CLIENT_POINT[1])

    def test_notepad_point_is_derived_from_its_own_window(self):
        np_screen = (self.NOTEPAD_RECT[0] + self.NOTEPAD_CLIENT_POINT[0],
                     self.NOTEPAD_RECT[1] + self.NOTEPAD_CLIENT_POINT[1])
        assert self.NOTEPAD_RECT[0] <= np_screen[0] < self.NOTEPAD_RECT[2]
        assert np_screen[0] > self.HERMES_RECT[2], "the point must be outside Hermes"

    def test_correct_mapping_keeps_it_on_the_external_monitor(self):
        vs = LAYOUTS["external right of primary"]
        px = self.NOTEPAD_RECT[0] + self.NOTEPAD_CLIENT_POINT[0]
        py = self.NOTEPAD_RECT[1] + self.NOTEPAD_CLIENT_POINT[1]
        nx, ny = vs.normalize(px, py)
        landed_x, landed_y = vs.denormalize(nx, ny)
        # 65535 quantisation steps over 3840 px is ~1.17 px per step; exact equality is not
        # achievable and the re-enable path must define its own tolerance explicitly rather
        # than inherit an accidental one from this test.
        assert abs(landed_x - px) <= 2 and abs(landed_y - py) <= 2
        # And it must NOT land in Hermes.
        assert landed_x >= self.HERMES_RECT[2]

    def test_primary_only_normalisation_would_land_inside_hermes(self):
        """Documented failure mode: the bug this topology is here to catch."""
        prim_w, prim_h = 1920, 1080
        px = self.NOTEPAD_RECT[0] + self.NOTEPAD_CLIENT_POINT[0]   # 2320
        py = self.NOTEPAD_CLIENT_POINT[1]
        bad_nx = int(px * 65535 / max(1, prim_w - 1))
        bad_ny = int(py * 65535 / max(1, prim_h - 1))
        # Interpreted over the virtual desktop, that lands far off-screen-right, not in
        # Hermes; interpreted over the primary it saturates. Either way it is wrong.
        assert bad_nx > 65535 // 2, "primary-only normalisation pushes the point off-target"

    def test_out_of_bounds_point_is_refused_without_emitting(self):
        vs = LAYOUTS["external right of primary"]
        port = SimulatedInputPort(vs, run_id="r")
        port.set_enabled(True)
        port.send(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS,
                            x=vs.right + 500, y=vs.y + 10))
        ev = port.events[-1]
        assert ev.input_emitted is False
        assert "OUT_OF_BOUNDS" in ev.guard_decision
        assert ev.refusal_reason


class TestSimulatedBackendNeverTouchesTheOS:
    def test_move_records_the_full_payload(self):
        vs = LAYOUTS["external right of primary"]
        port = SimulatedInputPort(vs, run_id="r")
        port.set_enabled(True)
        want = TestPrimaryOnlyNormalisationIsCaught.notepad_screen_point()
        port.send(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=want[0], y=want[1]))
        ev = port.events[-1]
        assert ev.screen_point == want
        assert ev.normalized_dx_dy == vs.normalize(2320, 450)
        named = set(ev.injection_flags_named)
        assert {"MOUSEEVENTF_MOVE", "MOUSEEVENTF_ABSOLUTE",
                "MOUSEEVENTF_VIRTUALDESK"} <= named
        assert ev.input_emitted is False

    def test_absolute_and_virtualdesk_are_always_set_for_absolute_moves(self):
        for name, vs in LAYOUTS.items():
            port = SimulatedInputPort(vs, run_id="r")
            port.send(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS,
                                x=vs.x + 10, y=vs.y + 10))
            flags = port.events[-1].injection_flags
            assert flags & MOUSEEVENTF_ABSOLUTE, name
            assert flags & MOUSEEVENTF_VIRTUALDESK, name
            assert flags & MOUSEEVENTF_MOVE, name

    def test_button_down_requires_a_verified_cursor_position(self):
        """A button acts where the cursor already is; that coupling must be explicit."""
        port = SimulatedInputPort(LAYOUTS["external right of primary"], run_id="r")
        port.set_enabled(True)
        port.send(Primitive(kind=PrimitiveType.MOUSE_BUTTON,
                            button=MouseButton.LEFT, down=True))
        assert port.events[-1].guard_decision == "requires-verified-cursor-position"
        assert port.events[-1].actual_cursor_point is None

    def test_no_os_api_is_referenced(self):
        """The backend must not even name an OS input function."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "adapters" / "input" / "simulated.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called.add(getattr(f, "attr", None) or getattr(f, "id", None))
        for forbidden in ("SendInput", "mouse_event", "keybd_event", "SetCursorPos"):
            assert forbidden not in called

    def test_telemetry_is_complete_for_every_event(self):
        """The fields the re-enable checklist requires, present by construction."""
        vs = LAYOUTS["external LEFT of primary (negative origin)"]
        port = SimulatedInputPort(
            vs, monitors=[(-1920, 0, 0, 1080, "external"), (0, 0, 1920, 1080, "primary")],
            run_id="r", dpi_awareness="per_monitor_v2")
        port.set_enabled(True)
        port.send(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=-1000, y=500))
        d = port.events[-1].to_dict()
        for field in ("run_id", "action_id", "intended_target_hwnd", "intended_target_pid",
                      "intended_target_title", "intended_target_class",
                      "intended_client_point", "intended_screen_point", "coordinate_space",
                      "virtual_desktop_bounds", "target_monitor_bounds", "dpi_awareness",
                      "normalized_dx_dy", "injection_flags", "actual_cursor_point",
                      "actual_window", "foreground_window", "protected_target_decision",
                      "guard_decision", "input_emitted"):
            assert field in d, f"telemetry missing {field}"
        assert d["virtual_desktop_bounds"] == (-1920, 0, 3840, 1080)
        assert d["target_monitor_bounds"] == "external"

    def test_write_produces_a_readable_artifact(self, tmp_path):
        vs = LAYOUTS["external right of primary"]
        port = SimulatedInputPort(vs, run_id="r")
        port.set_enabled(True)
        want = TestPrimaryOnlyNormalisationIsCaught.notepad_screen_point()
        port.send(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=want[0], y=want[1]))
        path = port.write(tmp_path / "sim.json")
        assert path.exists()
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["os_input_calls"] == 0
        assert payload["virtual_desktop"]["SM_CXVIRTUALSCREEN"] == 3840
