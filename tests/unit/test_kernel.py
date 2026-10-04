"""Kernel: states, ids, clock, redaction, event log."""

from __future__ import annotations

import pytest

from frameforge.kernel.bus import EventLog, Redactor
from frameforge.kernel.clock import FakeClock
from frameforge.kernel.events import EventKind
from frameforge.kernel.ids import canonical_json, hash_obj, new_run_id
from frameforge.kernel.states import (
    RunState,
    TransitionError,
    assert_transition,
    can_transition,
)


class TestStateMachine:
    def test_happy_path_transitions_are_allowed(self):
        assert can_transition(RunState.IDLE, RunState.ARMING)
        assert can_transition(RunState.ARMING, RunState.ACQUIRING_TARGET)
        assert can_transition(RunState.OBSERVING, RunState.DECIDING)
        assert can_transition(RunState.DECIDING, RunState.EXECUTING)
        assert can_transition(RunState.EXECUTING, RunState.VERIFYING)
        assert can_transition(RunState.OBSERVING, RunState.COMPLETED)

    def test_terminal_states_are_absorbing(self):
        for terminal in (RunState.COMPLETED, RunState.FAILED, RunState.UNKNOWN, RunState.ABORTED):
            for target in RunState:
                assert not can_transition(terminal, target), f"{terminal} -> {target}"

    def test_cannot_skip_from_idle_to_executing(self):
        assert not can_transition(RunState.IDLE, RunState.EXECUTING)
        with pytest.raises(TransitionError):
            assert_transition(RunState.IDLE, RunState.EXECUTING)

    def test_input_permitted_only_while_executing(self):
        """Only EXECUTING may send input. Anything else means the ports are no-ops."""
        from frameforge.kernel.states import INPUT_PERMITTED_STATES

        assert INPUT_PERMITTED_STATES == frozenset({RunState.EXECUTING})
        # ARMING must not be permitted: the grace countdown happens before ARMING ends.
        assert RunState.ARMING not in INPUT_PERMITTED_STATES


class TestIds:
    def test_run_id_is_sortable_and_prefixed(self):
        rid = new_run_id("2026-10-03T14:44:26.986+00:00")
        assert rid.startswith("ff-")
        # Lexicographic order must match chronological order, which is what makes
        # "latest run" a cheap directory scan.
        earlier = new_run_id("2026-10-03T14:44:26.986+00:00")
        later = new_run_id("2026-10-03T15:44:26.986+00:00")
        assert earlier < later

    def test_run_ids_are_unique(self):
        ids = {new_run_id("2026-10-03T14:44:26.986+00:00") for _ in range(50)}
        assert len(ids) == 50

    def test_canonical_json_is_key_order_independent(self):
        assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


class TestEventApiShape:
    """The event API is called from hundreds of sites; its shape must not drift silently.

    Two real bugs came from this: a keyword argument passed where a dict was expected, and
    leftover calls using an older ``(kind, component, summary)`` signature. Both were only
    found at runtime, deep in a run. These are static checks so they fail in CI instead.
    """

    def test_event_append_accepts_keyword_fields(self):
        log = EventLog(None, "r", clock=FakeClock())
        event = log.append(EventKind.FOCUS_LOST, "t", "msg", error="boom")
        assert event.data == {"error": "boom"}

    def test_director_event_takes_no_extra_positional_component(self):
        """``_event(kind, summary, **data)`` - the component is fixed to 'director'."""
        import ast
        import inspect
        from pathlib import Path

        from frameforge.kernel import director as director_module

        source = Path(inspect.getsourcefile(director_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_event"
            and len(n.args) > 2
        ]
        assert not offenders, f"_event called with a stray positional arg at lines {offenders}"

    def test_no_event_append_mixes_dict_and_kwargs(self):
        """A dict positional plus keywords is a shape change that binds wrongly."""
        import ast
        import inspect
        from pathlib import Path

        from frameforge.kernel import director as director_module

        source = Path(inspect.getsourcefile(director_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append" and len(node.args) >= 4 and node.keywords):
                offenders.append(node.lineno)
        assert not offenders, f"append() mixes a dict positional with kwargs at {offenders}"


class TestFakeClock:
    def test_sleep_advances_virtual_time(self):
        clock = FakeClock()
        clock.sleep_ms(500)
        assert clock.monotonic_ms() == 500
        assert clock.sleep_calls == [500]

    def test_advance_does_not_record_a_sleep(self):
        clock = FakeClock()
        clock.advance_ms(1000)
        assert clock.sleep_calls == []

    def test_negative_sleep_rejected(self):
        with pytest.raises(ValueError):
            FakeClock().sleep_ms(-1)


class TestRedactor:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwx",
            "sk-ant-abcdefghijklmnopqrstuvwx",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "4111111111111111",
        ],
    )
    def test_known_secret_shapes_are_scrubbed(self, secret: str):
        redactor = Redactor()
        assert secret not in redactor.scrub(f"the value is {secret} here")

    def test_bearer_token_scrubbed(self):
        redactor = Redactor()
        out = redactor.scrub("Authorization: Bearer abcdefghijklmnopqrstuv")
        assert "abcdefghijklmnopqrstuv" not in out

    def test_ordinary_text_untouched(self):
        redactor = Redactor()
        text = "health bar missing while hp below 30"
        assert redactor.scrub(text) == text

    def test_recursive_scrub_of_nested_structures(self):
        redactor = Redactor()
        payload = {"a": ["sk-abcdefghijklmnopqrstuvwx"], "b": {"c": "plain"}}
        out = redactor.scrub_obj(payload)
        assert "sk-abcdefghijklmnopqrstuvwx" not in str(out)
        assert out["b"]["c"] == "plain"

    def test_depth_limit_prevents_runaway(self):
        deep: dict = {"x": "leaf"}
        for _ in range(30):
            deep = {"n": deep}
        assert "TRUNCATED" in str(Redactor().scrub_obj(deep))

    def test_disabled_redactor_is_a_passthrough(self):
        assert Redactor(enabled=False).scrub("sk-abcdefghijklmnopqrstuvwx") == \
            "sk-abcdefghijklmnopqrstuvwx"


class TestEventLog:
    def test_append_only(self, tmp_path):
        log = EventLog(tmp_path / "e.jsonl", "r1", clock=FakeClock())
        log.append(EventKind.RUN_STARTED, "t", "one")
        log.append(EventKind.RUN_FINISHED, "t", "two")
        # No update/delete API exists at all; reading back must show both, in order.
        kinds = [e.kind for e in log.read_all()]
        assert kinds == [EventKind.RUN_STARTED, EventKind.RUN_FINISHED]
        assert not hasattr(log, "update") and not hasattr(log, "delete")

    def test_correction_appends_rather_than_rewrites(self, tmp_path):
        log = EventLog(tmp_path / "e.jsonl", "r1", clock=FakeClock())
        first = log.append(EventKind.VERIFIER_EVALUATED, "t", "first")
        log.append(EventKind.VERIFIER_EVALUATED, "t", "correction", supersedes=first.seq)
        events = log.read_all()
        assert len(events) == 2
        assert events[0].summary == "first"          # original intact
        assert events[1].supersedes == first.seq      # correction references it

    def test_payload_is_redacted_on_the_way_to_disk(self, tmp_path):
        path = tmp_path / "e.jsonl"
        log = EventLog(path, "r1", clock=FakeClock())
        log.append(EventKind.PLANNER_CALL, "t", "call", {"api_key": "sk-abcdefghijklmnopqrstuvwx"})
        assert log.redaction_hits == 1
        assert "sk-abcdefghijklmnopqrstuvwx" not in path.read_text(encoding="utf-8")

    def test_truncated_tail_is_tolerated(self, tmp_path):
        path = tmp_path / "e.jsonl"
        log = EventLog(path, "r1", clock=FakeClock())
        log.append(EventKind.RUN_STARTED, "t", "one")
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 99, "kind": "run.sta')  # simulate a crash mid-write
        assert len(EventLog(path, "r1").read_all()) == 1

    def test_keyword_fields_fold_into_data(self):
        """Extra keywords become data rather than a TypeError at the call site."""
        log = EventLog(None, "r1", clock=FakeClock())
        event = log.append(EventKind.PERCEPTION_DEGRADED, "t", "x",
                           occluded_fraction=0.8, detail="covered")
        assert event.data == {"occluded_fraction": 0.8, "detail": "covered"}

    def test_keyword_fields_merge_with_existing_data(self):
        log = EventLog(None, "r1", clock=FakeClock())
        event = log.append(EventKind.PERCEPTION_DEGRADED, "t", "x",
                           {"a": 1}, b=2)
        assert event.data == {"a": 1, "b": 2}

    def test_event_is_frozen(self, clock):
        log = EventLog(None, "r1", clock=clock)
        event = log.append(EventKind.RUN_STARTED, "t", "x")
        with pytest.raises(Exception):
            event.summary = "mutated"  # type: ignore[misc]

    def test_memory_is_bounded(self):
        log = EventLog(None, "r1", clock=FakeClock(), max_memory_events=10)
        for i in range(50):
            log.append(EventKind.CAPTURE_FRAME, "t", f"f{i}")
        assert len(log.events) == 10
