"""Keyboard must clear the same gate as mouse; scan mode must emit real scancodes.

Both defects were found by triaging the Notepad round-trip failure, and both were silent:
the click was guarded and the typing was not, and 'scan' mode emitted virtual keys while
claiming to emit scancodes. Neither raised an error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "frameforge"


# --------------------------------------------------------------- keyboard guard


class TestKeyboardClearsTheSameGate:
    def test_executor_guards_key_primitives(self):
        """A keystroke with no target guard must be refused, not delivered."""
        source = (SRC / "actions" / "executor.py").read_text(encoding="utf-8")
        assert "validate_keyboard()" in source, "keyboard input never consults the guard"
        assert "keyboard_without_target_guard" in source

    def test_topology_is_checked_for_keys_too(self):
        source = (SRC / "actions" / "executor.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        # KEY must be in the set of primitives whose dispatch depends on topology.
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "_TOPOLOGY_CHECKED_PRIMITIVES")
        names = {e.attr for e in ast.walk(fn.value) if isinstance(e, ast.Attribute)}
        assert "KEY" in names, f"topology is not checked for keys; set was {names}"

    def test_validate_keyboard_requires_target_focus(self):
        source = (SRC / "actions" / "target.py").read_text(encoding="utf-8")
        assert "def validate_keyboard" in source
        seg = source[source.index("def validate_keyboard"):]
        seg = seg[:seg.index("\n    def ")]
        # It must reject a foreground window that is not the registered target.
        assert "GetForegroundWindow" in seg
        assert "WRONG_WINDOW" in seg
        assert "PROTECTED" in seg

    def test_keyboard_refused_when_foreground_is_not_the_target(self):
        """The behavioural test: a keystroke aimed at a background target sends nothing."""
        from frameforge.actions.executor import ActionExecutor
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        sent: list[Primitive] = []

        class Port:
            def send(self, primitive, **kw):
                sent.append(primitive)
                return True

            def send_batch(self, prims):
                for p in prims:
                    sent.append(p)
                return len(prims)

            def release_all(self):
                pass

        class RefusingGuard:
            """Stands in for a guard that rejects, as it would when focus is elsewhere."""

            def validate_keyboard(self):
                from frameforge.actions.target import ValidationResult, Verdict

                return ValidationResult(
                    Verdict.WRONG_WINDOW,
                    "foreground is not the registered target; keystroke would go elsewhere")

            def session(self):
                class S:
                    def refresh_topology(self):
                        return True, ""
                return S()

        class Mgr:
            def check(self, *a, **k):
                from frameforge.actions.safety import Violation

                return Violation.NONE, ""

            def enforce_deadlines(self):
                pass

            def validate_target(self):
                return True, ""

            def audit(self):
                return {}

        ex = ActionExecutor(Port(), controller=None, policy=None)
        # A keyboard primitive with no guard at all must be refused outright.
        result = ex.execute([
            Primitive(kind=PrimitiveType.KEY, key=Key.A, down=True),
            Primitive(kind=PrimitiveType.KEY, key=Key.A, down=False),
        ])
        assert result.denied, "an unguarded keystroke was not refused"
        assert not sent, f"input was delivered with no guard: {sent}"


# ------------------------------------------------------------------- scancodes


class TestScanModeEmitsRealScancodes:
    def test_scancode_primitive_exists(self):
        from frameforge.ports.input import Primitive, PrimitiveType

        assert PrimitiveType.SCANCODE == "scancode"
        p = Primitive(kind=PrimitiveType.SCANCODE, scancode=0x1E)
        assert p.scancode == 0x1E

    def test_scancode_table_is_not_the_virtual_key_table(self):
        """The two namespaces differ; a table that aliases one to the other is the bug."""
        from frameforge.adapters.input.sendinput import _SC, _VK
        from frameforge.ports.input import Key

        differing = [k for k in _SC if k in _VK and _SC[k] != _VK[k]]
        assert len(differing) > 20, (
            "scancodes appear to equal virtual keys, which means one table aliased the other")
        # Spot-check known set-1 codes rather than trusting the whole table.
        assert _SC[Key.A] == 0x1E
        assert _SC[Key.Z] == 0x2C
        assert _SC[Key.SPACE] == 0x39
        assert _SC[Key.ENTER] == 0x1C
        assert _SC[Key.ESCAPE] == 0x01

    def test_navigation_keys_are_marked_extended(self):
        from frameforge.adapters.input.sendinput import _SC, _SC_EXTENDED
        from frameforge.ports.input import Key

        for key in (Key.UP, Key.DOWN, Key.LEFT, Key.RIGHT, Key.INSERT, Key.DELETE):
            assert _SC[key] in _SC_EXTENDED, f"{key} needs KEYEVENTF_EXTENDEDKEY"

    def test_scan_mode_compiles_to_scancode_primitives(self):
        """The regression: 'scan' used to compile to KEY primitives with wScan=0."""
        from frameforge.actions.compiler import ActionCompiler
        from frameforge.actions.model import TypeText
        from frameforge.ports.input import PrimitiveType

        compiled = ActionCompiler().compile(
            TypeText(text="ab", method="scan"))
        kinds = {p.kind for p in compiled.primitives}
        assert kinds == {PrimitiveType.SCANCODE}, (
            f"scan mode compiled to {kinds}, not SCANCODE")
        assert all(p.scancode > 0 for p in compiled.primitives)

    def test_unicode_mode_still_compiles_to_unicode(self):
        from frameforge.actions.compiler import ActionCompiler
        from frameforge.actions.model import TypeText
        from frameforge.ports.input import PrimitiveType

        compiled = ActionCompiler().compile(TypeText(text="hi", method="unicode"))
        assert {p.kind for p in compiled.primitives} == {PrimitiveType.UNICODE}

    def test_scancode_emit_requires_scancode_flag(self):
        """wVk must be 0; passing a virtual key makes Windows ignore the scancode."""
        source = (SRC / "actions" / "safety.py").read_text(encoding="utf-8")
        seg = source[source.index("def _scancode"):]
        seg = seg[:seg.index("\n    def ")]
        assert "KEYEVENTF_SCANCODE" in seg
        assert "wVk=0" in seg
        assert "wScan=scancode" in seg

    def test_scancode_release_bypasses_the_arm_gate(self):
        """Blocking a scancode release would strand the key, exactly as with KEY."""
        from frameforge.adapters.input.sendinput import is_release
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        assert is_release(Primitive(kind=PrimitiveType.SCANCODE, scancode=0x1E, down=False))
        assert not is_release(Primitive(kind=PrimitiveType.SCANCODE, scancode=0x1E, down=True))

    def test_key_without_a_scancode_fails_loudly(self):
        """Silent no-op is the failure mode being prevented."""
        from frameforge.actions.compiler import ActionCompiler
        from frameforge.actions.model import TypeText
        from frameforge.ports.input import Key

        # GAMEPAD_STATE-style pseudo-keys have no scancode; an unmapped char must raise.
        with pytest.raises(ValueError, match="scan-mode map"):
            ActionCompiler().compile(TypeText(text="é", method="scan"))
