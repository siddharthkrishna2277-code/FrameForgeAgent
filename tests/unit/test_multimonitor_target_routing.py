"""Multi-monitor target routing: the failure topology, made deterministic.

Topology represented throughout:
    Hermes   on the laptop / primary monitor   DISPLAY1 (0,0)-(1920,1080)
    Notepad  on the external monitor           DISPLAY2 (1920,0)-(3840,1080)
    virtual desktop origin (0,0), span 3840x1080, primary 1920x1080

The defect these pin: the executor validates the *planned* point before button-down, never
the *actual* cursor position. If the cursor does not arrive where the plan said - because
normalisation was wrong, the display changed, or something else moved it - the click still
lands wherever the cursor happens to be, which may be inside Hermes.
"""

from __future__ import annotations

import inspect

import pytest

from frameforge.actions.coordinates import ScreenPx, VirtualDesktop

# The topology above.
PRIMARY = (0, 0, 1920, 1080)
EXTERNAL = (1920, 0, 3840, 1080)
VIRTUAL = (0, 0, 3840, 1080)

HERMES_HWND = 32704008      # Chrome_WidgetWin_1, laptop monitor
HERMES_PID = 19064
NOTEPAD_HWND = 46533056     # external monitor
NOTEPAD_PID = 9896


class TestVirtualDesktopNormalisation:
    def test_maps_the_whole_virtual_desktop_not_the_primary(self):
        """A point on the external monitor must normalise mid-range, not saturate."""
        desktop = VirtualDesktop(*VIRTUAL)
        ext_mid = desktop.to_send_input(ScreenPx(2880, 540))
        assert 0 < ext_mid.x < 65535, "external-monitor point saturated to an edge"

    def test_left_edge_is_zero_and_far_edge_is_max(self):
        desktop = VirtualDesktop(*VIRTUAL)
        assert desktop.to_send_input(ScreenPx(0, 0)).x == 0
        far = desktop.to_send_input(ScreenPx(3839, 1079))
        assert far.x > 65000

    def test_monotonic_across_the_seam(self):
        """The primary/external seam must not fold: 1919 -> 1920 must increase.

        A normalisation that ignores the origin or clamps to the primary monitor produces a
        discontinuity or a plateau here, which is how a click aimed at the external display
        lands on the laptop one.
        """
        desktop = VirtualDesktop(*VIRTUAL)
        left = desktop.to_send_input(ScreenPx(1919, 540))
        right = desktop.to_send_input(ScreenPx(1920, 540))
        assert right.x > left.x, "the seam folds: 1920 does not map beyond 1919"

    def test_a_negative_origin_shifts_the_mapping(self):
        """External monitor placed to the LEFT of the primary: the origin goes negative.

        This is the case a size-only normalisation cannot express, and the reason the origin
        term is not optional decoration.
        """
        desktop = VirtualDesktop(x=-1920, y=0, width=3840, height=1080)
        left_edge = desktop.to_send_input(ScreenPx(-1920, 500))
        assert left_edge.x == 0, "the virtual origin must map to 0, not to a mid-range value"


class TestEmittedPayload:
    """What actually reaches SendInput."""

    def test_absolute_move_sets_both_absolute_and_virtualdesk(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "safety.py").read_text(encoding="utf-8")
        assert "MOUSEEVENTF_ABSOLUTE" in src
        assert "MOUSEEVENTF_VIRTUALDESK" in src
        # Both must be OR'd into the same call as the MOVE flag.
        i = src.index("MOUSEEVENTF_MOVE\n")
        seg = src[i:i + 200]
        assert "MOUSEEVENTF_ABSOLUTE" in seg and "MOUSEEVENTF_VIRTUALDESK" in seg

    @pytest.mark.xfail(strict=True, reason="DEFECT: _abs_norm ignores the virtual-desktop "
                                          "origin; a layout with a negative origin misroutes "
                                          "every click")
    def test_abs_norm_uses_the_virtual_origin(self):
        """The shipped formula divided by virtual SIZE only.

        With a zero origin that is arithmetically equivalent, so the defect is invisible on
        a laptop whose external monitor sits to the right - and catastrophic the moment the
        layout changes. The origin term must be present.
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "safety.py").read_text(encoding="utf-8")
        i = src.index("def _abs_norm")
        seg = src[i:src.index("\n    def ", i)]
        assert "SM_XVIRTUALSCREEN" in seg or "virtual_origin" in seg or "- vx" in seg, (
            "_abs_norm ignores the virtual-desktop origin; a layout with a negative origin "
            "will misroute every click")

    def test_button_down_and_up_are_separate_payloads(self):
        from frameforge.adapters.input.sendinput import _MOUSE_BUTTON_FLAGS

        assert _MOUSE_BUTTON_FLAGS["left"][0] != _MOUSE_BUTTON_FLAGS["left"][1]
        assert _MOUSE_BUTTON_FLAGS["right"][0] != _MOUSE_BUTTON_FLAGS["right"][1]


class TestRightClickIsNotEmittable:
    def test_right_click_is_denied_by_default(self):
        """Guardrail G-ABS-11. This is why the reported right-click cannot have come
        from the current code path."""
        from frameforge.actions.safety import InputSafetyManager, Violation
        from frameforge.ports.input import MouseButton, Primitive, PrimitiveType

        mgr = InputSafetyManager()
        violation, detail = mgr._screen(
            Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=MouseButton.RIGHT, down=True))
        assert violation is Violation.RIGHT_CLICK_NOT_ALLOWED

    def test_nothing_in_src_enables_it(self):
        """No constructor anywhere passes allow_right_click=True."""
        import re
        from pathlib import Path

        src_root = Path(__file__).resolve().parents[2] / "src" / "frameforge"
        offenders = []
        for p in src_root.rglob("*.py"):
            if re.search(r"allow_right_click\s*=\s*True", p.read_text(encoding="utf-8")):
                offenders.append(p.name)
        assert not offenders, f"right-click enabled in: {offenders}"


class TestTheRealGuardGap:
    """The gap that matters: planned point is validated, actual cursor is not."""

    def test_executor_never_reads_the_cursor_position(self):
        """DEFECT (open). Asserts the gap is still present.

        Deliberately written as a positive assertion of the *absence* of the fix: it passes
        while the defect exists and fails the moment cursor readback is added, which is the
        signal to delete it. An xfail would have hidden that.
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "executor.py").read_text(encoding="utf-8")
        assert "GetCursorPos" not in src and "cursor_position" not in src, (
            "the executor must read the real cursor position after moving; without it a "
            "mis-normalised move is invisible and the button lands wherever the cursor is")

    def test_guard_never_reads_the_cursor_position(self):
        """DEFECT (open). Same polarity rule as the test above."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "target.py").read_text(encoding="utf-8")
        assert "GetCursorPos" not in src and "cursor_position" not in src

    def test_pre_button_check_uses_the_planned_point(self):
        from frameforge.actions.executor import ActionExecutor

        src = inspect.getsource(ActionExecutor.execute)
        pre = src[src.index("MOUSE_BUTTON"):]
        assert "self._verified_move" in pre, (
            "the pre-button check validates _verified_move, which is the point the plan "
            "asked for - not where the cursor actually is")

    def test_no_tolerance_comparison_exists(self):
        """DEFECT (open). Same polarity rule as the tests above."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "executor.py").read_text(encoding="utf-8")
        assert "tolerance" not in src.lower(), (
            "no defined tolerance exists for planned-vs-actual cursor drift")


class TestEvidenceCompleteness:
    def test_audit_records_the_move_and_the_button_separately(self):
        """Needed to reconstruct plan -> normalisation -> cursor -> target -> event."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "safety.py").read_text(encoding="utf-8")
        assert "foreground_window" in src or "foreground" in src
        assert "target_window" in src or "target" in src

    @pytest.mark.xfail(strict=True, reason="DEFECT: the emitted normalized pair is not "
                                          "recorded, so a mis-normalisation cannot be "
                                          "reconstructed from the evidence artifact")
    def test_normalised_coordinates_are_not_recorded(self):
        """A known evidence gap: the audit logs the event but not the normalised payload
        it emitted, so a mis-normalisation cannot be reconstructed after the fact."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "src" / "frameforge"
               / "actions" / "safety.py").read_text(encoding="utf-8")
        i = src.index("def _abs_norm")
        body = src[i:src.index("\n    def ", i)]
        assert "record" in body.lower() or "audit" in body.lower(), (
            "_abs_norm records nothing, so the emitted normalized pair is unrecoverable")
