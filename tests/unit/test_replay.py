"""Deterministic replay: plan round-trip and diffing."""

from __future__ import annotations

import json

import pytest

from frameforge.actions.model import Click, Hotkey, Intent, KeyPress, MouseLook, Scroll, Wait
from frameforge.ports.geometry import Point
from frameforge.ports.input import Key, MouseButton
from frameforge.tasks.replay import (
    PlanEntry,
    RecordedPlanner,
    action_from_dict,
    diff_plans,
    load_plan,
)
from frameforge.store.runs import RunPaths


class TestActionRoundTrip:
    @pytest.mark.parametrize("action", [
        Wait(ms=250),
        Intent(intent="jump"),
        KeyPress(key=Key.SPACE),
        Hotkey(keys=[Key.LCTRL, Key.S]),
        Click(at=Point(100, 200), button=MouseButton.RIGHT, count=2),
        Scroll(dx=0, dy=-3),
        MouseLook(dx=120, dy=-40),
    ])
    def test_action_survives_serialise_deserialise(self, action):
        """A plan that cannot be replayed is not a deterministic record."""
        restored = action_from_dict(action.model_dump(mode="json"))
        assert restored.describe() == action.describe()
        assert restored.model_dump(mode="json") == action.model_dump(mode="json")

    def test_unknown_action_type_is_refused(self):
        with pytest.raises(ValueError, match="unknown action type"):
            action_from_dict({"type": "launch_missiles"})

    def test_surface_relative_click_survives(self):
        original = Click(at=Point(33, 44))
        restored = action_from_dict(original.model_dump(mode="json"))
        assert restored.at.as_tuple() == (33, 44)


class TestDiff:
    def test_identical_plans(self):
        a = [PlanEntry("s1", Wait(ms=10), "c0")]
        b = [PlanEntry("s1", Wait(ms=10), "c0")]
        assert diff_plans(a, b).identical

    def test_different_action_is_reported_with_both_sides(self):
        a = [PlanEntry("s1", Intent(intent="jump"), "c2")]
        b = [PlanEntry("s1", Intent(intent="crouch"), "c2")]
        diff = diff_plans(a, b)
        assert not diff.identical
        assert len(diff.differences) == 1
        d = diff.differences[0]
        assert "jump" in d["recorded"] and "crouch" in d["replayed"]

    def test_missing_action_is_reported(self):
        a = [PlanEntry("s1", Wait(ms=10), "c0"), PlanEntry("s2", Wait(ms=10), "c0")]
        b = [PlanEntry("s1", Wait(ms=10), "c0")]
        diff = diff_plans(a, b)
        assert not diff.identical
        assert diff.differences[0]["reason"] == "missing action in replay"

    def test_extra_action_is_reported(self):
        a = [PlanEntry("s1", Wait(ms=10), "c0")]
        b = [PlanEntry("s1", Wait(ms=10), "c0"), PlanEntry("s2", Wait(ms=10), "c0")]
        diff = diff_plans(a, b)
        assert diff.differences[0]["reason"] == "extra action in replay"

    def test_describe_is_useful(self):
        a = [PlanEntry("s1", Intent(intent="jump"), "c2")]
        b = [PlanEntry("s1", Intent(intent="crouch"), "c2")]
        text = diff_plans(a, b).describe()
        assert "DRIFT" in text and "jump" in text and "crouch" in text

    def test_serialisable(self):
        a = [PlanEntry("s1", Wait(ms=10), "c0")]
        json.dumps(diff_plans(a, []).to_dict())


def _request(step_name: str):
    from frameforge.ports.planner import PerceptionSummary, PlanContext, PlanRequest

    return PlanRequest(
        context=PlanContext(objective="t", current_step=step_name),
        perception=PerceptionSummary(mono_ms=0.0, frame_index=1, frame_hash="h",
                                     surface_size=(10, 10)),
    )


class TestRecordedPlanner:
    def test_replays_the_action_recorded_for_this_step(self):
        entries = [PlanEntry("s1", Wait(ms=5), "c0"), PlanEntry("s2", Wait(ms=6), "c0")]
        planner = RecordedPlanner(entries)
        assert planner.propose(_request("s1")).steps[0].action.ms == 5
        assert planner.propose(_request("s2")).steps[0].action.ms == 6

    def test_abstains_when_no_action_was_recorded_for_the_step(self):
        """A step added since recording has no action; abstaining beats inventing one."""
        entries = [PlanEntry("s1", Wait(ms=5), "c0")]
        planner = RecordedPlanner(entries)
        proposal = planner.propose(_request("brand_new_step"))
        assert proposal.abstain and "no recorded action" in proposal.abstain_reason

    def test_abstains_when_exhausted(self):
        planner = RecordedPlanner([])
        assert planner.propose(_request("s1")).abstain

    def test_declares_no_network(self):
        caps = RecordedPlanner([]).capabilities()
        assert caps.requires_network is False


class TestLoadPlan:
    def test_missing_plan_is_a_clear_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="plan.jsonl"):
            load_plan(RunPaths(root=tmp_path / "nope"))

    def test_round_trips_through_a_file(self, tmp_path):
        paths = RunPaths(root=tmp_path / "r")
        paths.ensure()
        original = [
            {"step": "s1", "action": Intent(intent="jump").describe(),
             "action_record": Intent(intent="jump").model_dump(mode="json"), "blast": "c2"},
            {"step": "s2", "action": Click(at=Point(5, 6)).describe(),
             "action_record": Click(at=Point(5, 6)).model_dump(mode="json"), "blast": "c1"},
        ]
        paths.plan.write_text(
            "\n".join(json.dumps(e) for e in original) + "\n", encoding="utf-8"
        )
        entries = load_plan(paths)
        assert [e.step for e in entries] == ["s1", "s2"]
        assert entries[0].action.describe() == "intent(jump)"
        assert entries[1].action.describe() == Click(at=Point(5, 6)).describe()
