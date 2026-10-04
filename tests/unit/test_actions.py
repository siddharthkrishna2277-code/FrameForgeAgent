"""Actions: model validation, compilation, hold balance, text mapping."""

from __future__ import annotations

import pytest

from frameforge.actions.compiler import ActionCompiler, Binding, ControlProfile, check_blast_allowed
from frameforge.actions.executor import drag_primitives, hotkey_primitives, mouselook_primitives
from frameforge.actions.model import (
    AI_AUTHORABLE,
    BlastClass,
    Click,
    Hotkey,
    Intent,
    KeyDown,
    KeyPress,
    KeyUp,
    HoldTracker,
    MouseLook,
    TypeText,
)
from frameforge.actions.textmap import char_to_key, is_typable, text_to_keys
from frameforge.kernel.errors import BlastRadiusError, SchemaValidationError
from frameforge.ports.geometry import Point, Size
from frameforge.ports.input import Key, MouseButton, PrimitiveType


class TestActionModel:
    def test_blast_classes_are_sensible_defaults(self):
        assert Click(at=Point(1, 1)).blast() is BlastClass.REVERSIBLE
        assert KeyDown(key=Key.W).blast() is BlastClass.SUSTAINED
        assert KeyUp(key=Key.W).blast() is BlastClass.OBSERVE
        # Typing lands somewhere, so it is treated as an external effect by default.
        assert TypeText(text="hi").blast() is BlastClass.EXTERNAL

    def test_hotkey_rejects_duplicate_keys(self):
        with pytest.raises(ValueError):
            Hotkey(keys=[Key.A, Key.A])

    def test_intent_name_is_validated(self):
        with pytest.raises(ValueError):
            Intent(intent="rm -rf /")

    def test_c4_is_never_authorable_by_ai(self):
        assert BlastClass.FORBIDDEN not in AI_AUTHORABLE
        assert BlastClass.EXTERNAL not in AI_AUTHORABLE

    def test_mouselook_bounds_are_enforced(self):
        with pytest.raises(ValueError):
            MouseLook(dx=0, dy=0, steps=0)
        with pytest.raises(ValueError):
            MouseLook(dx=0, dy=0, duration_ms=999_999)


class TestHoldTracker:
    def test_balanced_state_passes(self):
        tracker = HoldTracker()
        tracker.key_down(Key.W, 0)
        tracker.key_up(Key.W, 10)
        tracker.assert_balanced(11)

    def test_leaked_hold_is_detected(self):
        tracker = HoldTracker()
        tracker.key_down(Key.W, 0)
        with pytest.raises(RuntimeError, match="unbalanced"):
            tracker.assert_balanced(10)

    def test_release_plan_includes_everything(self):
        tracker = HoldTracker()
        tracker.key_down(Key.W, 0)
        tracker.button_down(MouseButton.LEFT, 0)
        names = [n for n, _h in tracker.release_plan()]
        assert "key:w" in names and "btn:left" in names
        assert tracker.held_count == 2

    def test_button_leak_detected(self):
        tracker = HoldTracker()
        tracker.button_down(MouseButton.LEFT, 0)
        with pytest.raises(RuntimeError):
            tracker.assert_balanced()


class TestPrimitiveBuilders:
    def test_hotkey_releases_in_reverse_order(self):
        prims = hotkey_primitives([Key.LCTRL, Key.S])
        keys = [(p.key, p.down) for p in prims if p.kind == PrimitiveType.KEY]
        pressed = [k for k, d in keys if d]
        released = [k for k, d in keys if not d]
        assert pressed == [Key.LCTRL, Key.S]
        # Reverse release matters: games that track modifier state get confused otherwise.
        assert released == [Key.S, Key.LCTRL]

    def test_drag_is_balanced(self):
        prims = drag_primitives([Point(0, 0), Point(10, 10), Point(20, 20)])
        buttons = [p.down for p in prims if p.kind == PrimitiveType.MOUSE_BUTTON]
        assert buttons == [True, False]

    def test_drag_needs_two_points(self):
        with pytest.raises(ValueError):
            drag_primitives([Point(0, 0)])

    def test_mouselook_sums_to_requested_delta(self):
        prims = mouselook_primitives(100, -50, steps=10, duration_ms=100, curve="ease")
        assert sum(p.dx for p in prims) == 100
        assert sum(p.dy for p in prims) == -50

    def test_mouselook_is_interruptible(self):
        """Multiple sub-moves means an estop mid-flick can actually stop the flick."""
        prims = mouselook_primitives(400, 400, steps=8)
        assert len(prims) > 1


class TestControlProfile:
    def test_unbound_intent_is_a_compile_error(self):
        """Not a silent no-op: a forgotten binding must fail loudly."""
        compiler = ActionCompiler(ControlProfile(name="empty"))
        with pytest.raises(SchemaValidationError, match="not bound"):
            compiler.compile(Intent(intent="jump"))

    def test_intent_compiles_to_bound_key(self):
        control = ControlProfile(name="c")
        control.bind("jump", Binding(kind="key", keys=(Key.SPACE,)))
        compiler = ActionCompiler(control)
        compiled = compiler.compile(Intent(intent="jump"))
        assert len(compiled.primitives) == 2
        assert compiled.primitives[0].key is Key.SPACE

    def test_intent_respects_sensitivity(self):
        control = ControlProfile(name="c", look_sensitivity=0.5)
        compiler = ActionCompiler(control)
        compiled = compiler.compile(MouseLook(dx=100, dy=0))
        assert sum(p.dx for p in compiled.primitives) == 50

    def test_click_at_point_becomes_absolute_move_then_click(self):
        compiler = ActionCompiler(ControlProfile(name="c"))
        compiled = compiler.compile(Click(at=Point(10, 20)))
        assert compiled.primitives[0].kind == PrimitiveType.MOUSE_MOVE_ABS
        assert compiled.primitives[0].x == 10

    def test_double_click_emits_two_pairs(self):
        compiler = ActionCompiler(ControlProfile(name="c"))
        compiled = compiler.compile(Click(at=Point(5, 5), count=2))
        buttons = [p for p in compiled.primitives if p.kind == PrimitiveType.MOUSE_BUTTON]
        assert len(buttons) == 4

    def test_surface_offset_is_applied_for_second_monitor(self):
        """The multi-monitor correctness case: a click must not land on the wrong screen."""
        from frameforge.ports.capture import Surface, SurfaceKind

        surface = Surface(
            kind=SurfaceKind.MONITOR, size=Size(1920, 1080), offset_x=1920, offset_y=0
        )
        compiler = ActionCompiler(ControlProfile(name="c"), surface=surface)
        compiled = compiler.compile(Click(at=Point(100, 100)))
        assert compiled.primitives[0].x == 2020  # 100 + 1920 offset

    def test_key_down_expects_hold(self):
        compiler = ActionCompiler(ControlProfile(name="c"))
        assert compiler.compile(KeyDown(key=Key.W)).expects_hold is True
        assert compiler.compile(KeyUp(key=Key.W)).expects_hold is False


class TestBlastRadius:
    def test_forbidden_is_always_rejected(self):
        with pytest.raises(BlastRadiusError):
            check_blast_allowed(BlastClass.FORBIDDEN, ai_authored=False, allow_irreversible=True)

    def test_external_requires_explicit_consent(self):
        with pytest.raises(BlastRadiusError, match="allow_irreversible"):
            check_blast_allowed(BlastClass.EXTERNAL, ai_authored=False, allow_irreversible=False)

    def test_external_allowed_with_consent(self):
        check_blast_allowed(BlastClass.EXTERNAL, ai_authored=False, allow_irreversible=True)

    def test_ai_may_not_author_external_even_with_consent(self):
        """Enforced on the action, not the author. Provenance is never trusted."""
        with pytest.raises(BlastRadiusError, match="AI"):
            check_blast_allowed(BlastClass.EXTERNAL, ai_authored=True, allow_irreversible=True)

    def test_ai_may_author_ordinary_actions(self):
        check_blast_allowed(BlastClass.REVERSIBLE, ai_authored=True, allow_irreversible=False)
        check_blast_allowed(BlastClass.SUSTAINED, ai_authored=True, allow_irreversible=False)


class TestTextMapping:
    def test_plain_characters(self):
        assert char_to_key("a") == (Key.A, False)
        assert char_to_key("1") == (Key.N1, False)

    def test_shifted_characters_need_shift(self):
        assert char_to_key("A") == (Key.A, True)
        assert char_to_key("!") == (Key.N1, True)

    def test_unknown_character_is_refused_not_guessed(self):
        with pytest.raises(ValueError):
            char_to_key("\u00e9")

    def test_typable_predicate(self):
        assert is_typable("Hello 123")
        assert not is_typable("caf\u00e9")

    def test_expansion_balances_shift(self):
        keys = text_to_keys("Ab")
        assert keys.count(Key.LSHIFT) == 2
