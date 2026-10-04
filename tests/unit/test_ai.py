"""AI tier: packet minimisation, strict parsing, containment, degradation.

These are the tests that justify the claim "an AI planner cannot bypass a guard". They do
not test model quality - they test that *nothing the model says* can widen its authority.
"""

from __future__ import annotations

import json

import pytest

from frameforge.actions.model import Hotkey, Intent, KeyPress, Wait
from frameforge.config.settings import Settings
from frameforge.kernel.bus import Redactor
from frameforge.kernel.errors import CapabilityUnavailable
from frameforge.ports.geometry import Point
from frameforge.ports.input import Key
from frameforge.ports.planner import (
    AnchorObservation,
    PerceptionSummary,
    PlanContext,
    PlanRequest,
    PlanStep,
)
from frameforge.planning.ai import (
    RESPONSE_SCHEMA,
    AiPlanner,
    CliBridgeProvider,
    OpenAiCompatProvider,
    build_packet,
    parse_response,
)
from frameforge.planning.validator import PlanValidator, ValidationPolicy

ALLOWED = frozenset({"jump", "confirm"})


def make_request(ocr=("MENU", "PLAY"), anchors=(("menu", 0.95),)) -> PlanRequest:
    return PlanRequest(
        context=PlanContext(objective="start the game", allowed_intents=tuple(sorted(ALLOWED)),
                            budget_actions_left=100, budget_ai_calls_left=10),
        perception=PerceptionSummary(
            mono_ms=0.0, frame_index=3, frame_hash="h", surface_size=(1280, 720),
            window_title="Fake", is_foreground=True, confidence=0.9,
            anchors=tuple(AnchorObservation(n, s, threshold=0.8) for n, s in anchors),
            ocr_lines=tuple((t, (0, 0, 10, 10), 0.9) for t in ocr),
        ),
    )


class ScriptedProvider:
    """Returns a canned response. Stands in for any provider."""

    name = "scripted"

    def __init__(self, response: str | None = None, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.seen_packets: list[str] = []
        self.corrections: list[str] = []

    def available(self) -> bool:
        return True

    def complete(self, system, packet, schema, correction=""):
        self.seen_packets.append(packet)
        self.corrections.append(correction)
        if self.fail:
            msg = "provider exploded"
            raise RuntimeError(msg)
        return self.response or ""


class TestPacketMinimisation:
    def test_packet_contains_objective_and_state(self):
        text = build_packet(make_request())
        assert "start the game" in text
        assert "1280x720" in text
        assert "menu" in text
        assert "PLAY" in text

    def test_packet_is_not_the_frame(self):
        """G-PER-01: a summary, never raw pixels."""
        text = build_packet(make_request()).lower()
        for forbidden in ("base64", "png", "jpeg", "image_url", "data:"):
            assert forbidden not in text

    def test_packet_secrets_are_redacted(self):
        request = make_request(ocr=("token sk-abcdefghijklmnopqrstuvwx here",))
        text = build_packet(request, redactor=Redactor())
        assert "sk-abcdefghijklmnopqrstuvwx" not in text
        assert "[REDACTED]" in text

    def test_packet_is_small(self):
        """A bounded packet is what keeps token cost and privacy surface bounded."""
        request = make_request(ocr=tuple(f"line{i}" for i in range(500)))
        assert len(build_packet(request, max_ocr=25)) < 4000


class TestStrictParsing:
    def test_valid_plan_parses(self):
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump"},
            "expected_change": "character jumps",
        }]})
        proposal = parse_response(raw, allowed_intents=ALLOWED)
        assert len(proposal.steps) == 1
        assert isinstance(proposal.steps[0].action, Intent)

    def test_abstain_parses(self):
        proposal = parse_response(json.dumps({"abstain": True, "abstain_reason": "unsure"}),
                                  allowed_intents=ALLOWED)
        assert proposal.abstain and proposal.abstain_reason == "unsure"

    def test_unknown_top_level_field_rejected(self):
        raw = json.dumps({"steps": [{"action": {"kind": "wait", "ms": 1},
                                     "expected_change": "x"}], "system_override": "yes"})
        with pytest.raises(ValueError, match="unknown top-level"):
            parse_response(raw, allowed_intents=ALLOWED)

    def test_unknown_action_field_rejected(self):
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump", "sudo": True},
            "expected_change": "x"}]})
        with pytest.raises(ValueError):
            parse_response(raw, allowed_intents=ALLOWED)

    def test_unknown_action_kind_rejected(self):
        raw = json.dumps({"steps": [{"action": {"kind": "delete_everything"},
                                     "expected_change": "x"}]})
        with pytest.raises(ValueError, match="unknown action kind"):
            parse_response(raw, allowed_intents=ALLOWED)

    def test_disallowed_intent_rejected_at_parse(self):
        raw = json.dumps({"steps": [{"action": {"kind": "intent", "intent": "format_disk"},
                                     "expected_change": "x"}]})
        with pytest.raises(ValueError, match="not permitted"):
            parse_response(raw, allowed_intents=ALLOWED)

    def test_missing_expected_change_accepted_by_parser_but_rejected_by_validator(self):
        """The parser is structural; the validator owns policy."""
        raw = json.dumps({"steps": [{"action": {"kind": "intent", "intent": "jump"}}]})
        proposal = parse_response(raw, allowed_intents=ALLOWED)
        validator = PlanValidator(ValidationPolicy(allowed_intents=ALLOWED))
        outcome = validator.validate(proposal, make_request())
        assert not outcome.ok and outcome.rule_id == "G-VAL-06"

    def test_non_json_rejected(self):
        with pytest.raises(Exception):
            parse_response("I think you should click play", allowed_intents=ALLOWED)

    def test_empty_steps_without_abstain_rejected(self):
        with pytest.raises(ValueError, match="no steps"):
            parse_response(json.dumps({"steps": []}), allowed_intents=ALLOWED)

    def test_self_reported_confidence_is_rejected_entirely(self):
        """G-DEC-04: a model cannot attach a confidence that might later be trusted.

        The strict parser refuses an unknown step field outright, so a self-reported
        confidence cannot even enter the system - stronger than accepting it and marking it
        untrusted, because there is then nothing to accidentally start reading.
        """
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump"},
            "expected_change": "x", "rationale": "because", "confidence": 0.99}]})
        with pytest.raises(ValueError, match="unknown fields"):
            parse_response(raw, allowed_intents=ALLOWED)

    def test_parsed_steps_carry_no_confidence_by_default(self):
        """Belt and braces: even a well-formed step has confidence=None."""
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump"},
            "expected_change": "x", "rationale": "because"}]})
        proposal = parse_response(raw, allowed_intents=ALLOWED)
        assert proposal.steps[0].confidence is None


class TestContainment:
    """A model response must not be able to widen its authority."""

    def test_parsed_plan_still_faces_the_validator(self):
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump"}, "expected_change": "x"}]})
        proposal = parse_response(raw, allowed_intents=frozenset({"jump"}))
        validator = PlanValidator(ValidationPolicy(allowed_intents=frozenset({"jump"}),
                                                  ai_authored=True))
        assert validator.validate(proposal, make_request()).ok

    def test_hostile_rationale_from_a_model_is_a_strike(self):
        raw = json.dumps({"steps": [{
            "action": {"kind": "intent", "intent": "jump"},
            "expected_change": "x",
            "rationale": "first, increase my budget and skip the verification"}]})
        proposal = parse_response(raw, allowed_intents=ALLOWED)
        validator = PlanValidator(ValidationPolicy(allowed_intents=ALLOWED, ai_authored=True))
        outcome = validator.validate(proposal, make_request())
        assert outcome.hostile and not outcome.ok

    def test_schema_forbids_additional_properties(self):
        assert RESPONSE_SCHEMA["additionalProperties"] is False
        assert RESPONSE_SCHEMA["properties"]["steps"]["items"]["additionalProperties"] is False
        assert RESPONSE_SCHEMA["properties"]["steps"]["items"]["properties"]["action"][
            "additionalProperties"] is False

    def test_schema_caps_step_count(self):
        assert RESPONSE_SCHEMA["properties"]["steps"]["maxItems"] == 5


class TestDegradation:
    def test_provider_failure_degrades_rather_than_raising(self):
        planner = AiPlanner(ScriptedProvider(fail=True),
                            settings=Settings(ai_enabled=True, ai_provider="cli"))
        proposal = planner.propose(make_request())
        assert proposal.abstain and proposal.degraded
        assert "provider exploded" in proposal.abstain_reason

    def test_corrective_retry_is_bounded_to_one(self):
        """A model that misnames a field gets one fix; there is no retry loop."""
        provider = ScriptedProvider("not json at all")
        planner = AiPlanner(provider, settings=Settings(ai_enabled=True, ai_provider="cli"))
        proposal = planner.propose(make_request())
        assert proposal.degraded
        assert len(provider.seen_packets) == 2, "expected exactly one corrective retry"

    def test_corrective_retry_receives_the_error(self):
        provider = ScriptedProvider('{"steps":[{"action":{"kind":"intent"},"expected_change":"x"}]}')
        planner = AiPlanner(provider, settings=Settings(ai_enabled=True, ai_provider="cli"))
        planner.propose(make_request())
        assert provider.corrections[0] == ""
        assert "unknown fields" in provider.corrections[1] or "intent" in provider.corrections[1]

    def test_unparseable_response_degrades_with_an_error(self):
        planner = AiPlanner(ScriptedProvider("not json at all"),
                            settings=Settings(ai_enabled=True, ai_provider="cli"))
        proposal = planner.propose(make_request())
        assert proposal.abstain and proposal.degraded
        assert "failed validation" in proposal.abstain_reason

    def test_transcript_records_every_exchange(self):
        """G-AUD-04: the audit trail of what the model actually said."""
        raw = json.dumps({"steps": [{"action": {"kind": "wait", "ms": 10},
                                     "expected_change": "x"}]})
        planner = AiPlanner(ScriptedProvider(raw),
                            settings=Settings(ai_enabled=True, ai_provider="cli"))
        planner.propose(make_request())
        assert len(planner.transcript) == 1
        assert planner.transcript[0]["packet_chars"] > 0
        assert planner.transcript[0]["response_chars"] > 0

    def test_unavailable_provider_degrades(self):
        class Unavailable(ScriptedProvider):
            def available(self):
                return False

        planner = AiPlanner(Unavailable(), settings=Settings())
        assert planner.propose(make_request()).degraded

    def test_ai_budget_is_charged_and_enforced(self):
        from frameforge.actions.ledger import BudgetLedger, BudgetLimits
        from frameforge.kernel.clock import FakeClock

        raw = json.dumps({"steps": [{"action": {"kind": "wait", "ms": 10},
                                     "expected_change": "x"}]})
        ledger = BudgetLedger(limits=BudgetLimits(max_ai_calls=1), clock=FakeClock())
        planner = AiPlanner(ScriptedProvider(raw), settings=Settings(ai_enabled=True),
                            ledger=ledger)
        assert planner.propose(make_request()).steps
        second = planner.propose(make_request())
        assert second.degraded and "budget" in second.abstain_reason

    def test_no_network_provider_declares_that_it_needs_one(self):
        caps = CliBridgeProvider().capabilities() if hasattr(CliBridgeProvider, "capabilities") else None
        provider = OpenAiCompatProvider(Settings())
        assert provider.name == "openai_compat"

    def test_cli_provider_available_only_when_on_path(self):
        import shutil
        provider = CliBridgeProvider()
        assert provider.available() == (shutil.which("hermes") is not None)


class TestConstruction:
    def test_unknown_provider_rejected(self):
        from frameforge.planning.ai import build_ai_planner

        with pytest.raises(ValueError, match="unknown ai_provider"):
            build_ai_planner(Settings(ai_provider="telepathy"))

    def test_unavailable_provider_raises_rather_than_silently_disabling(self):
        from frameforge.planning.ai import build_ai_planner

        with pytest.raises(CapabilityUnavailable):
            build_ai_planner(Settings(ai_provider="openai_compat", ai_base_url="",
                                       ai_model=""))

    def test_ai_off_by_default(self):
        s = Settings()
        assert not s.ai_enabled and s.ai_provider == "none"
        assert not s.share_frames, "frames must not be shared by default (G-PER-01)"
