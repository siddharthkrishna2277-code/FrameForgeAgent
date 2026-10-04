"""Bounded context, adapted from a study of mature agent systems.

The rule being enforced is simple and easy to violate accidentally: a planner acting on a
silently-truncated view cannot tell that its view was incomplete, and will confidently
conclude "nothing was on screen" when in fact the text did not fit.
"""

from __future__ import annotations

from frameforge.actions.budget import Budget, build_packet_budget


class TestBudget:
    def test_counts_tokens(self):
        b = Budget()
        assert b.add("a", "x" * 400) is True
        assert b.used_tokens == 100

    def test_rejects_an_oversized_item(self):
        b = Budget(max_tokens=10_000, max_item_tokens=100)
        assert b.add("huge", "x" * 2000) is False
        assert b.dropped and b.oversized

    def test_required_item_is_never_dropped_for_size(self):
        """A required fragment that does not fit is recorded, not silently trimmed."""
        b = Budget(max_tokens=10_000, max_item_tokens=10)
        assert b.add("objective", "x" * 1000, required=True) is False
        assert b.oversized and not b.dropped

    def test_drops_when_the_ceiling_is_reached(self):
        b = Budget(max_tokens=120, max_item_tokens=100)
        assert b.add("a", "x" * 400)
        assert b.add("b", "x" * 400) is False
        assert b.dropped

    def test_required_fragments_bypass_the_ceiling(self):
        b = Budget(max_tokens=120, max_item_tokens=100)
        b.add("a", "x" * 400)
        assert b.add("objective", "y" * 40, required=True) is True

    def test_drop_is_attributed(self):
        """A drop must name what was lost, or the caller cannot act on it."""
        b = Budget(max_tokens=100, max_item_tokens=50)
        b.add("keep", "x" * 40)
        b.add("drop", "y" * 400)
        assert any("drop" in d for d in b.dropped)
        # Oversized items are attributed separately, with their measured cost.
        assert any("drop" in o for o in b.oversized)
        assert any("t)" in o for o in b.oversized)

    def test_add_all_is_deterministic(self):
        """The same input must produce the same packet, or a recorded run cannot replay."""
        fragments = {"objective": "o", "state": "s", "budget": "b", "anchors": "a", "text": "t"}
        b1 = Budget(max_tokens=10_000)
        b2 = Budget(max_tokens=10_000)
        b1.add_all(fragments, required=("objective", "state", "budget"))
        b2.add_all(fragments, required=("objective", "state", "budget"))
        assert [n for n, _ in b1.items] == [n for n, _ in b2.items]

    def test_required_are_added_first(self):
        b = Budget(max_tokens=10_000)
        b.add_all({"a": "a", "objective": "o"}, required=("objective",))
        assert b.items[0][0] == "objective"

    def test_report_is_serialisable_and_complete(self):
        import json

        b = Budget(max_tokens=50)
        b.add("a", "x" * 40)
        b.add("b", "y" * 400)
        report = json.loads(json.dumps(b.report()))
        for key in ("max_tokens", "used_tokens", "utilisation", "items", "dropped"):
            assert key in report

    def test_describe_is_human_readable(self):
        b = Budget(max_tokens=100)
        b.add("a", "x" * 40)
        assert "tokens" in b.describe()

    def test_preset_is_modest(self):
        """A distilled view on purpose: the planner should not depend on detail."""
        b = build_packet_budget()
        assert b.max_tokens <= 8000


class TestPacketIsBounded:
    def test_packet_reports_truncation(self):
        from frameforge.planning.ai import build_packet
        from frameforge.ports.planner import (
            AnchorObservation, PerceptionSummary, PlanContext, PlanRequest)

        request = PlanRequest(
            context=PlanContext(objective="x", budget_actions_left=5,
                                budget_ai_calls_left=1),
            perception=PerceptionSummary(
                mono_ms=0.0, frame_index=1, frame_hash="h", surface_size=(1280, 720),
                anchors=tuple(AnchorObservation(f"a{i}", 0.9) for i in range(80)),
                ocr_lines=tuple((f"line {i} " * 40, (0, 0, 10, 10), 0.9) for i in range(60)),
            ),
        )
        packet = build_packet(request, max_tokens=300)
        # It must fit the ceiling, and it must say when it did not.
        assert len(packet) // 4 <= 400, f"packet exceeded its own ceiling: {len(packet)} chars"
        assert "truncated" in packet or "omitted" in packet

    def test_small_packet_is_not_annotated(self):
        from frameforge.planning.ai import build_packet
        from frameforge.ports.planner import PerceptionSummary, PlanContext, PlanRequest

        request = PlanRequest(
            context=PlanContext(objective="start", budget_actions_left=5,
                                budget_ai_calls_left=1),
            perception=PerceptionSummary(mono_ms=0.0, frame_index=1, frame_hash="h",
                                         surface_size=(1280, 720)),
        )
        packet = build_packet(request)
        assert "truncated" not in packet
        assert "objective: start" in packet
