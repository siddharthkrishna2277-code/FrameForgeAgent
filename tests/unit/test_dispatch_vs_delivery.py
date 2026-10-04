"""Dispatched is not delivered. Both facts must be reported, separately.

The preflight of the Notepad POC produced a run whose audit trail said `"sent": 2`,
`"sent": 3`, `"sent": 12` - while `--dry-run` guaranteed nothing reached Windows. A reader
checking "did input reach the operator's machine" would have read the opposite of the
truth. The count was of primitives the executor forwarded, not events Windows received.

These tests pin the distinction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "frameforge"


class TestTheTwoFactsAreSeparate:
    def test_execution_result_has_both(self):
        from frameforge.actions.executor import ExecutionResult

        r = ExecutionResult(0, 0.0)
        assert hasattr(r, "primitives_sent")
        assert hasattr(r, "delivered_to_os")
        assert r.primitives_sent == 0 and r.delivered_to_os == 0

    def test_tool_result_reports_delivery(self):
        from frameforge.actions.controller import ToolResult

        t = ToolResult(ok=True, action_id="a1", delivered=3)
        assert t.to_dict()["delivered"] == 3

    def test_ok_alone_does_not_imply_delivery(self):
        """A disarmed port returns ok=True with delivered=0. That must be expressible."""
        from frameforge.actions.controller import ToolResult

        t = ToolResult(ok=True, action_id="a1")
        assert t.ok is True
        assert t.delivered == 0
        assert t.outcome == "executed" or t.outcome == ""


class TestNoMisleadingFieldNames:
    def test_director_does_not_emit_a_bare_sent_field(self):
        source = (SRC / "kernel" / "director.py").read_text(encoding="utf-8")
        assert "sent=result.primitives_sent" not in source, (
            "a bare 'sent' key reads as OS delivery, which it is not")
        assert "dispatched=result.primitives_sent" in source
        assert "delivered_to_os=result.delivered_to_os" in source

    def test_no_primitives_sent_key_in_the_audit_payload(self):
        source = (SRC / "kernel" / "director.py").read_text(encoding="utf-8")
        assert '"primitives_sent" =' not in source


class TestDeclarableActions:
    """Every primitive the compiler can emit must have a declarative action.

    A primitive with no declarative equivalent is refused as "undeclarable", which is
    correct - but if the compiler can emit a kind the executor cannot describe, that kind
    is permanently unusable. Two such gaps existed and both were silent.
    """

    @pytest.mark.parametrize("method,expected", [
        ("unicode", "UNICODE"),
        ("scan", "SCANCODE"),
    ])
    def test_text_methods_compile_to_describable_primitives(self, method, expected):
        from frameforge.actions.model import TypeText
        from frameforge.ports.input import PrimitiveType

        from frameforge.actions.compiler import ActionCompiler

        compiled = ActionCompiler().compile(TypeText(text="ab", method=method))
        assert compiled.primitives
        for p in compiled.primitives:
            assert p.kind.name == expected

    def test_unicode_primitive_becomes_a_typetext_action(self):
        """UNICODE had no _action_for case, so all unicode typing was unauthorisable."""
        # Scope to _action_for: PrimitiveType.UNICODE appears elsewhere in the file, and
        # a whole-file search would find the wrong occurrence.
        source = (SRC / "actions" / "executor.py").read_text(encoding="utf-8")
        body = source[source.index("def _action_for"):]
        body = body[:body.index("\n    def ")]
        assert "PrimitiveType.UNICODE" in body
        assert "TypeText" in body

    def test_scancode_primitive_carries_its_declarative_key(self):
        """A scancode with no Key cannot be authorised; the key must ride along."""
        from frameforge.actions.model import TypeText
        from frameforge.ports.input import PrimitiveType

        from frameforge.actions.compiler import ActionCompiler

        compiled = ActionCompiler().compile(TypeText(text="ab", method="scan"))
        for p in compiled.primitives:
            assert p.kind is PrimitiveType.SCANCODE
            assert p.scancode_key is not None, (
                "a scancode with no declarative key would be refused as undeclarable")

    def test_every_primitive_kind_is_either_describable_or_intentionally_not(self):
        """Guard against the next kind being added without a declarative action."""
        source = (SRC / "actions" / "executor.py").read_text(encoding="utf-8")
        body = source[source.index("def _action_for"):]
        body = body[:body.index("\n    def ")]
        from frameforge.ports.input import PrimitiveType

        for kind in PrimitiveType:
            # GAMEPAD_STATE is documented as a feature-flagged no-op.
            if kind.name == "GAMEPAD_STATE":
                continue
            assert f"PrimitiveType.{kind.name}" in body, (
                f"{kind.name} has no declarative action; it would be refused as undecla"
                "rable the moment anything emitted it")


class TestDryRunDeliversNothing:
    def test_sendinput_port_short_circuits_before_the_safety_manager(self):
        source = (SRC / "adapters" / "input" / "sendinput.py").read_text(encoding="utf-8")
        body = source[source.index("    def send(self, primitive: Primitive,"):]
        body = body[:body.index("\n    def ")]
        dry = body.index("if self._dry_run:")
        safety = body.index("self._safety.send")
        assert dry < safety, "dry-run must return before the safety manager is reached"
        assert "return False" in body[dry:dry + 60]
