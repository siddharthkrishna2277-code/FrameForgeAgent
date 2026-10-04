"""A popup overlay owned by the target is not a legitimate click or keyboard target.

Found live. A Notepad ``PopupHost`` was open over the editor for an entire run: the
scenario's ``focus_the_editor`` click landed on the menu, 56 scancodes were delivered to a
window whose keyboard focus was an ``InputSiteWindowClass``, and the verifier spent several
runs reporting menu items ("Select all", "Delete", "Undo") as document text.

The guard permitted it because ownership was the only test. Every popup a target opens
passes same-pid and same-root checks, so ownership says nothing about whether the surface is
the one the scenario planned against.
"""

from __future__ import annotations

import pytest

from frameforge.actions.target import (
    _POPUP_CLASS_MARKERS,
    Verdict,
    _is_transient_overlay,
)


class TestTransientOverlayDetection:
    @pytest.mark.parametrize("cls", [
        "Microsoft.UI.Content.PopupWindowSiteBridge",   # the exact live class
        "PopupHost",
        "Windows.UI.Composition.PopupWindow",
        "ComboBoxEx32",
        "TOOLTIPS_CLASS32",
    ])
    def test_known_overlay_classes_are_detected(self, cls):
        assert _is_transient_overlay({"class_name": cls}) is True

    def test_the_live_class_is_covered(self):
        """Pin the exact class observed live, so a rename is noticed."""
        assert _is_transient_overlay(
            {"class_name": "Microsoft.UI.Content.PopupWindowSiteBridge"}) is True

    @pytest.mark.parametrize("cls", [
        "Notepad",
        "RichEditD2DPT",
        "NotepadTextBox",
        "Chrome_WidgetWin_1",
        "Shell_TrayWnd",
    ])
    def test_real_surfaces_are_not_overlays(self, cls):
        assert _is_transient_overlay({"class_name": cls}) is False

    def test_title_markers_catch_popuphost(self):
        assert _is_transient_overlay(
            {"class_name": "Something", "root_title": "PopupHost"}) is True

    def test_classic_menu_is_caught_by_title_not_class(self):
        """#32768 is the *title* of a classic Win32 menu window, not a class name."""
        assert _is_transient_overlay(
            {"class_name": "SomeClass", "root_title": "#32768"}) is True

    def test_none_is_not_an_overlay(self):
        assert _is_transient_overlay(None) is False
        assert _is_transient_overlay({}) is False

    def test_markers_are_lowercase_for_case_insensitive_matching(self):
        for marker in _POPUP_CLASS_MARKERS:
            assert marker == marker.lower(), f"{marker} must be lowercase to match reliably"


class TestTheGuardRefusesIt:
    def test_point_verdict_exists(self):
        assert Verdict.POPUP_OVERLAY.value == "popup_overlay_at_point"

    def test_validate_point_checks_the_overlay_before_ownership(self):
        """The order matters: an overlay owned by the target passes ownership, so the
        overlay test must come first or it is never reached for owned popups."""
        import inspect

        from frameforge.actions.target import TargetGuard

        src = inspect.getsource(TargetGuard.validate_point)
        assert src.index("_is_transient_overlay") < src.index("protected_pids")

    def test_validate_keyboard_checks_the_overlay(self):
        import inspect

        from frameforge.actions.target import TargetGuard

        src = inspect.getsource(TargetGuard.validate_keyboard)
        assert "_is_transient_overlay" in src, (
            "an open popup takes keyboard focus; the keyboard path must refuse it too")
        assert "GetFocus" in src

    def test_both_paths_emit_the_same_verdict(self):
        import inspect

        from frameforge.actions.target import TargetGuard

        for name in ("validate_point", "validate_keyboard"):
            src = inspect.getsource(getattr(TargetGuard, name))
            assert "Verdict.POPUP_OVERLAY" in src, f"{name} does not use the overlay verdict"
