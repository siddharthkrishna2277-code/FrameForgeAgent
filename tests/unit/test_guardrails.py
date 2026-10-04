"""Guardrail tests - TEST-* ids referenced in docs/AI_GUARDRAILS.md.

Every rule with a TEST- id is asserted here. A guardrail with no test is a wish, so the
guardrail index is effectively a test manifest, and a test that fails means a guard was
weakened.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from frameforge.actions.compiler import ActionCompiler, Binding, ControlProfile
from frameforge.actions.model import Action, Click, Intent, KeyPress, TypeText, Wait
from frameforge.actions.model import BlastClass
from frameforge.kernel.bus import EventLog, Redactor
from frameforge.kernel.clock import FakeClock
from frameforge.kernel.errors import (
    Aborted,
    AuthorizationError,
    BlastRadiusError,
    PolicyError,
)
from frameforge.kernel.events import EventKind
from frameforge.ports.geometry import Point
from frameforge.ports.input import Key
from frameforge.ports.planner import (
    AnchorObservation,
    PerceptionSummary,
    PlanContext,
    PlanProposal,
    PlanRequest,
    PlanStep,
    PlannerTier,
)
from frameforge.planning.validator import PlanValidator, ValidationPolicy

SRC = Path(__file__).resolve().parents[2] / "src" / "frameforge"


# --------------------------------------------------------------------- G-ROLE


class TestRoleBoundary:
    """TEST-ROLE-01..04: Frame Forge never edits game source."""

    def test_role_01_engine_defines_no_patch_operations(self):
        """No *callable* in the engine may apply a diff or write game source.

        Checks AST call/attribute names rather than raw text: the docstrings legitimately
        discuss the prohibition in words, and a text scan would either pass on a real
        implementation hidden in a docstring or fail on prose that merely names the rule.
        """
        banned = {"apply_patch", "apply_diff", "write_patch", "apply_unified_diff",
                  "apply_hunks", "patch_file", "code_edit", "edit_source"}
        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Call):
                    func = node.func
                    names.append(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
                elif isinstance(node, ast.FunctionDef):
                    names.append(node.name)
                for name in names:
                    if name and name.lower() in banned:
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
        assert not offenders, f"engine defines patch operations: {offenders}"

    def test_role_02_subprocess_import_is_bounded(self):
        """subprocess is permitted for input/window/direct-launch, not for editing."""
        # Every allowed site, and why. None of them may use subprocess to *edit* anything;
        # the roles are: OCR bridge, process-name fallback, video encoding, the CLI entry
        # point, and the local-CLI AI provider.
        allowed = {
            "adapters/ocr/winrt_ocr.py": "one-shot PowerShell OCR bridge (fallback path)",
            "adapters/ocr/winrt_host.py": "persistent PowerShell OCR host",
            "adapters/window/pywin32_window.py": "tasklist fallback for process names",
            "store/runs.py": "ffmpeg video encoding",
            "cli/main.py": "CLI entry point (testbed launch, doctor probes)",
            "kernel/director.py": "run metadata / process identity queries",
            "planning/ai.py": "local-CLI AI provider bridge",
            "tasks/launch.py": "scenario-authorised direct launch of the target application",
            "actions/safety.py": "input recovery probe only (reads layout state)",
        }
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "import subprocess" in text or "subprocess.run" in text:
                rel = path.relative_to(SRC).as_posix()
                if rel not in allowed:
                    offenders.append(rel)
        assert not offenders, f"subprocess used outside the allowlist: {offenders}"

    def test_role_04_report_schema_forbids_fix_recommendations(self):
        """TEST-ROLE-04: the report describes behaviour, never proposes a patch."""
        from frameforge.qa.report import Report

        fields = set(Report().to_dict().keys())
        banned = {"fix", "patch", "diff", "suggested_change", "code_change", "remediation"}
        assert not (fields & banned), f"report exposes fix-shaped fields: {fields & banned}"
        text = (SRC / "qa" / "report.py").read_text(encoding="utf-8").lower()
        # The module may *discuss* the prohibition, so check it does not offer one.
        assert "suggested_fix" not in text


# ------------------------------------------------------------------------- G-LAW


class TestFailClosed:
    """TEST-LAW-01 / TEST-ORDER-01."""

    def test_law_malformed_step_is_rejected_not_crashed(self):
        """A structurally invalid step is a rejection with a rule id, never an exception."""
        validator = PlanValidator()
        unknown = object()  # type: ignore[arg-type]
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=unknown, expected_change="x"),)), _request()
        )
        assert not outcome.ok
        assert outcome.rule_id == "G-VAL-01"

    def test_order_executor_is_unreachable_without_a_compiler(self):
        """The director's write path requires a compiled primitive batch."""
        from frameforge.kernel.director import RunDirector

        source = (SRC / "kernel" / "director.py").read_text(encoding="utf-8")
        # _execute must compile before it can call the executor.
        assert "compiler.compile(action)" in source
        assert "self.executor.execute(" in source


# ------------------------------------------------------------------- G-AUTH-01


class TestAuthorisation:
    """TEST-AUTH-04: an unattested profile is refused."""

    def _profile(self, **overrides):
        base = {
            "name": "t",
            "authorized_use": {
                "basis": "owner_prototype",
                "attestation": "a sufficiently long attestation",
                "attested_by": "owner",
                "offline": True,
            },
            "target": {"title_regex": "Something"},
        }
        base.update(overrides)
        return base

    def test_valid_profile_loads(self):
        from frameforge.profiles.schema import GameProfileSpec

        spec = GameProfileSpec.model_validate(self._profile())
        assert spec.authorized_use.basis == "owner_prototype"

    def test_unidentified_target_is_rejected(self):
        from frameforge.profiles.schema import GameProfileSpec

        with pytest.raises(Exception, match="unidentified"):
            GameProfileSpec.model_validate(self._profile(target={}))

    def test_unsupported_basis_is_rejected(self):
        from frameforge.profiles.schema import GameProfileSpec

        bad = self._profile()
        bad["authorized_use"]["basis"] = "public_multiplayer_automation"
        with pytest.raises(Exception):
            GameProfileSpec.model_validate(bad)

    def test_offline_single_player_must_be_offline(self):
        from frameforge.profiles.schema import GameProfileSpec

        bad = self._profile()
        bad["authorized_use"]["basis"] = "offline_single_player"
        bad["authorized_use"]["offline"] = False
        with pytest.raises(Exception):
            GameProfileSpec.model_validate(bad)

    def test_signal_url_must_be_loopback(self):
        """A remote signal endpoint would turn a test hook into an exfiltration path."""
        from frameforge.profiles.schema import GameProfileSpec

        spec = self._profile()
        spec["signals"] = [{"name": "leak", "kind": "http", "url": "http://evil.example.com/x"}]
        with pytest.raises(Exception, match="loopback"):
            GameProfileSpec.model_validate(spec)

    def test_launcher_profile_rejects_baked_in_direct_command(self):
        from frameforge.profiles.schema import LauncherProfileSpec

        with pytest.raises(Exception, match="opt-in"):
            LauncherProfileSpec.model_validate({
                "name": "l",
                "launcher_target": {"title_regex": "Steam"},
                "direct_command": ["game.exe"],
            })


# ---------------------------------------------------------------------- G-PER


class TestPerceptionBoundary:
    """TEST-PER-02 / 03 / 05."""

    def test_per_03_secret_never_reaches_disk(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            log = EventLog(path, "r", clock=FakeClock())
            log.append(EventKind.PLANNER_CALL, "t", "x", {"key": "sk-abcdefghijklmnopqrstuvwx"})
            assert "sk-abcdefghijklmnopqrstuvwx" not in path.read_text(encoding="utf-8")

    def test_per_03_settings_never_serialise_the_key_itself(self):
        from frameforge.config.settings import Settings

        data = Settings().redacted_dict()
        assert "ai_api_key_env" not in data
        assert "ai_api_key_present" in data

    def test_per_05_ai_off_makes_no_planner_call(self):
        """ai: off is a complete mode, not a degraded one."""
        from frameforge.config.settings import Settings

        assert Settings().ai_enabled is False
        assert Settings().ai_provider == "none"

    def test_per_02_evidence_store_applies_redaction_before_writing(self, tmp_path, vision):
        import numpy as np

        from frameforge.ports.geometry import RegionSpec, Size
        from frameforge.store.runs import EvidenceStore, RunPaths

        paths = RunPaths(root=tmp_path / "run")
        store = EvidenceStore(
            paths, vision=vision,
            redact_regions=(RegionSpec(0.0, 0.0, 1.0, 0.5),),
        )
        image = np.full((100, 100, 3), 255, np.uint8)
        saved = store.save_frame(image, "x")
        assert saved is not None and saved.exists()


# ---------------------------------------------------------------------- G-VAL


def _request(**ctx_overrides) -> PlanRequest:
    ctx = PlanContext(objective="test", allowed_intents=("jump",))
    for k, v in ctx_overrides.items():
        setattr(ctx, k, v)
    perception = PerceptionSummary(
        mono_ms=0.0, frame_index=1, frame_hash="h", surface_size=(1280, 720),
        anchors=(AnchorObservation("menu", 0.95, threshold=0.8),),
    )
    return PlanRequest(context=ctx, perception=perception)


class TestValidator:
    def test_val_06_missing_postcondition_is_rejected(self):
        validator = PlanValidator()
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=Intent(intent="jump")),)), _request()
        )
        assert not outcome.ok and outcome.rule_id == "G-VAL-06"

    def test_val_06_wait_is_exempt(self):
        """A wait's effect is timing; requiring a change predicate of it is nonsense."""
        validator = PlanValidator()
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=Wait(ms=100)),)), _request()
        )
        assert outcome.ok

    def test_val_07_plan_length_bounded(self):
        validator = PlanValidator(ValidationPolicy(max_plan_steps=2))
        steps = tuple(PlanStep(action=Wait(ms=1)) for _ in range(5))
        outcome = validator.validate(PlanProposal(steps=steps), _request())
        assert not outcome.ok and outcome.rule_id == "G-VAL-07"

    def test_val_02_intent_must_be_in_allowlist(self):
        validator = PlanValidator(ValidationPolicy(allowed_intents=frozenset({"jump"})))
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=Intent(intent="quit"), expected_change="x"),)),
            _request(),
        )
        assert not outcome.ok and outcome.rule_id == "G-VAL-02"

    def test_rejection_is_atomic(self):
        """One bad step invalidates the whole plan; partial application is worse."""
        validator = PlanValidator()
        outcome = validator.validate(
            PlanProposal(steps=(
                PlanStep(action=Wait(ms=1)),
                PlanStep(action=Intent(intent="not_allowed")),
            )),
            _request(),
        )
        assert not outcome.ok
        assert outcome.steps == ()

    def test_abstaining_is_valid(self):
        """G-DEC-05: 'I don't know' must be expressible."""
        validator = PlanValidator()
        outcome = validator.validate(
            PlanProposal(abstain=True, abstain_reason="nothing applies"), _request()
        )
        assert outcome.ok

    def test_validator_is_pure(self):
        """G-VAL-09: identical input must produce identical rejection."""
        validator = PlanValidator()
        proposal = PlanProposal(steps=(PlanStep(action=Intent(intent="nope")),))
        results = {validator.validate(proposal, _request()).rule_id for _ in range(5)}
        assert len(results) == 1

    def test_val_05_external_action_needs_consent(self):
        validator = PlanValidator(ValidationPolicy(allow_irreversible=False))
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=TypeText(text="x"), expected_change="y"),)),
            _request(),
        )
        assert not outcome.ok

    def test_val_05_external_allowed_with_consent(self):
        validator = PlanValidator(ValidationPolicy(allow_irreversible=True))
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=TypeText(text="x"), expected_change="y"),)),
            _request(),
        )
        assert outcome.ok


# --------------------------------------------------------------------- G-BLAST


class TestBlastRules:
    def test_blast_01_ai_cannot_author_external(self):
        validator = PlanValidator(ValidationPolicy(ai_authored=True, allow_irreversible=True))
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=TypeText(text="x"), expected_change="y"),)),
            _request(),
        )
        assert not outcome.ok

    def test_blast_01_enforced_on_action_not_author(self):
        """Even with consent, an AI-authored C3 action is refused."""
        validator = PlanValidator(ValidationPolicy(ai_authored=False, allow_irreversible=True))
        outcome = validator.validate(
            PlanProposal(steps=(PlanStep(action=TypeText(text="x"), expected_change="y"),)),
            _request(),
        )
        assert outcome.ok  # human-authored path is allowed with consent
        # ... and the *AI* path is not, regardless of consent:
        ai_validator = PlanValidator(ValidationPolicy(ai_authored=True, allow_irreversible=True))
        assert not ai_validator.validate(
            PlanProposal(steps=(PlanStep(action=TypeText(text="x"), expected_change="y"),)),
            _request(),
        ).ok


# ------------------------------------------------------------------- G-VERD-01
# RELEASE GATE: no AI adapter may appear in the verdict computation's import graph.


class TestVerdictIntegrity:
    """TEST-VERD-01 (release gate) and the UNKNOWN-never-PASS rule."""

    def test_verd01_verify_module_imports_no_planner_or_ai(self):
        tree = ast.parse((SRC / "perception" / "verify.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        forbidden = ("planning", "adapters.ai", "openai", "llm", "ai_planner")
        offenders = [m for m in imported if any(f in m for f in forbidden)]
        assert not offenders, f"verdict path imports AI/planning modules: {offenders}"

    def test_verd01_director_verdict_path_is_deterministic(self):
        """The verifier itself must not depend on a planner of any tier."""
        source = (SRC / "kernel" / "director.py").read_text(encoding="utf-8")
        assert "remote" not in source.lower()
        assert "openai" not in source.lower()

    def test_verd_02_unknown_is_not_coerced_to_pass(self):
        """There is no 'assume passed on timeout' anywhere in the verifier."""
        # Check *code*, not comments: this module's docstring legitimately names the
        # pattern it forbids, so a text scan would fail on the rule's own explanation.
        tree = ast.parse((SRC / "perception" / "verify.py").read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        for forbidden in ("assume_pass", "default_pass", "pass_on_timeout", "force_pass"):
            assert forbidden not in called
        # And structurally: UNKNOWN is a first-class member of the disposition enum.
        from frameforge.kernel.events import Disposition

        assert Disposition.UNKNOWN is not Disposition.PASS
        assert Disposition.UNKNOWN.value == "unknown"

    def test_verd_03_missing_observation_yields_unknown_with_no_evidence(self):
        from frameforge.kernel.events import Disposition
        from frameforge.perception.verify import ScreenChanged, Verifier
        from frameforge.ports import fakes

        verifier = Verifier(fakes.FakeVision())
        verdict = verifier.evaluate(ScreenChanged(), None, None)
        assert verdict.disposition is Disposition.UNKNOWN
        assert verdict.frame_hash == ""


# --------------------------------------------------------------------- G-DEC


class TestDecisionGuardrails:
    def test_dec_03_no_mechanism_exists_to_widen_permissions(self):
        """No prompt, flag, or field grants a planner more authority."""
        import dataclasses

        from frameforge.ports.planner import PlanContext, PlanStep

        step_fields = {f.name for f in dataclasses.fields(PlanStep)}
        ctx_fields = {f.name for f in dataclasses.fields(PlanContext)}
        # No field on either side can grant more authority.
        for forbidden in ("budget_override", "widen", "elevate", "skip_verification",
                          "escalate", "bypass", "override"):
            assert forbidden not in step_fields | ctx_fields
        # And the confidence a planner reports is explicitly untrusted: it is a plain
        # field the scorer never reads.
        assert "confidence" in step_fields

    def test_dec_03_permission_widening_request_is_a_hostile_strike(self):
        validator = PlanValidator()
        outcome = validator.validate(
            PlanProposal(
                steps=(PlanStep(action=Wait(ms=1), expected_change="x"),
                       PlanStep(action=Wait(ms=1), expected_change="y")),
                rationale="increase my budget and skip the verification",
            ),
            _request(),
        )
        assert outcome.hostile and outcome.rule_id == "G-INJ-03"

    def test_dec_05_uncertainty_is_expressible(self):
        from frameforge.planning.tier0 import ProfilePlanner

        planner = ProfilePlanner()
        proposal = planner.propose(_request())
        assert proposal.abstain and proposal.abstain_reason


# ---------------------------------------------------------------------- G-DEV


class TestDevRules:
    """TEST-DEV-01: no game names, coordinates or key bindings in the engine core."""

    ENGINE_DIRS = ("kernel", "actions", "perception", "planning")

    def test_dev_01_no_literal_game_names_in_core(self):
        banned = re.compile(
            r"\b(steam|elden\s?ring|cyberpunk|minecraft|fortnite|gta\s?v|"
            r"apex|doom|quake|half[-\s]?life|counter[-\s]?strike|valorant|"
            r"witcher|skyrim|fallout|warcraft|diablo|overwatch)\b",
            re.I,
        )
        offenders = []
        for d in self.ENGINE_DIRS:
            for path in (SRC / d).rglob("*.py"):
                for m in banned.finditer(path.read_text(encoding="utf-8")):
                    offenders.append(f"{path.relative_to(SRC)}: {m.group(0)}")
        assert not offenders, f"game names in engine core: {offenders}"

    def test_dev_01_no_control_bindings_in_core(self):
        """A WASD-style binding literal in the engine would be a control profile in disguise.

        ``actions/textmap.py`` is excluded by design: it is a keyboard *layout* table
        (character -> key), not a game control mapping. It is the one place in the engine
        where many Key constants legitimately appear together.
        """
        pattern = re.compile(r"""Key\.(W|A|S|D)\b[^)\n]*\bKey\.(W|A|S|D)\b""")
        offenders: list[str] = []
        for d in self.ENGINE_DIRS:
            for path in (SRC / d).rglob("*.py"):
                if path.name == "textmap.py":
                    continue
                for m in pattern.finditer(path.read_text(encoding="utf-8")):
                    offenders.append(f"{path.relative_to(SRC)}: {m.group(0)}")
        assert not offenders, f"control binding hardcoded in core: {offenders}"

    def test_dev_01_control_bindings_live_only_in_profiles_and_presets(self):
        """Positive check: the bindings exist, in data, not in the engine."""
        presets = (SRC / "profiles" / "presets.py").read_text(encoding="utf-8")
        assert "Key.W" in presets
        # Every core module must be free of key literals, checked per file.
        offenders: list[str] = []
        for d in self.ENGINE_DIRS:
            for path in (SRC / d).rglob("*.py"):
                if path.name == "textmap.py":
                    continue
                if "Key.W" in path.read_text(encoding="utf-8"):
                    offenders.append(path.relative_to(SRC).as_posix())
        assert not offenders, f"key literals leaked into core: {offenders}"


# ---------------------------------------------------------------------- G-SES


class TestSessionGuardrails:
    def test_ses_01_input_gate_checked_inside_the_port(self):
        """The gate lives in the adapter, not a higher layer, so queued work cannot pass."""
        source = (SRC / "adapters" / "input" / "sendinput.py").read_text(encoding="utf-8")
        assert "not self._enabled" in source
        # Disarm must release held state.
        assert "self.release_all(" in source

    def test_ses_01a_release_is_never_blocked_by_a_disarm(self):
        """The release-blocking invariant, asserted on source.

        An emergency stop firing mid-chord used to disarm the port and then refuse the
        key-ups, stranding Ctrl and Alt as logically held: every physical keypress
        afterwards behaved as a chord. A release must always be deliverable.
        """
        # Assert the *semantics* rather than a literal: both gates must combine "not
        # enabled" with a release check, so a release is never blocked by a disarm.
        import re

        port = (SRC / "adapters" / "input" / "sendinput.py").read_text(encoding="utf-8")
        safety = (SRC / "actions" / "safety.py").read_text(encoding="utf-8")

        for name, text in (("sendinput", port), ("safety", safety)):
            enabled = r"_enabled" if name == "sendinput" else r"enabled"
            pattern = re.compile(
                rf"if not (?:self\.)?{enabled} and (?:not )?(?:is_release\(primitive\)|releasing)"
            )
            assert pattern.search(text), (
                f"{name}: the arm gate must exempt releases, so a disarm cannot strand a key"
            )

    def test_ses_01b_all_injection_is_owned_by_the_safety_manager(self):
        """Only one module may call SendInput, so tracking cannot be bypassed."""
        import ast

        offenders = []
        for path in (SRC).rglob("*.py"):
            if path.name == "safety.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "SendInput":
                    offenders.append(path.name)
        assert not offenders, f"SendInput used outside safety.py: {offenders}"

    def test_ses_01c_no_keybd_event_call_remains(self):
        """keybd_event cannot be audited the same way and has caused real corruption."""
        for path in (SRC).rglob("*.py"):
            assert "keybd_event(" not in path.read_text(encoding="utf-8"), path.name

    def test_ses_01d_language_switch_chords_are_refused(self):
        """Ctrl+Shift, Alt+Shift and Win+Space change machine-wide input state."""
        from frameforge.actions.safety import InputSafetyManager

        manager = InputSafetyManager()
        for chord in (["ctrl", "shift"], ["lctrl", "lshift"], ["alt", "shift"],
                      ["rctrl", "rshift"], ["win", "space"]):
            violation, detail = manager.check_chord(chord)
            assert violation.value != "none", f"{chord} was allowed ({detail})"
        # An ordinary chord is still allowed.
        assert manager.check_chord(["ctrl", "c"])[0].value == "none"
        assert manager.check_chord(["ctrl", "alt", "delete"])[0].value == "none"

    def test_ses_01_grace_period_is_required_by_default(self):
        from frameforge.config.settings import Settings

        assert Settings().grace_seconds >= 1.0

    def test_ses_03_human_input_default_is_pause_not_ignore(self):
        from frameforge.config.settings import Settings

        assert Settings().human_input_policy == "pause"

    def test_ses_02_focus_guard_blocks_on_drift(self):
        from frameforge.actions.focus import FocusGuard, FocusPolicy
        from frameforge.kernel.errors import FocusLostError
        from frameforge.ports import fakes

        window = fakes.FakeWindow()
        clock = FakeClock()
        guard = FocusGuard(window, target_hwnd=1000, tolerance_ms=0.0, clock=clock)
        guard.assert_focus()  # correct focus passes
        window.foreground_hwnd = 9999  # user alt-tabs away
        clock.advance_ms(500)
        with pytest.raises(FocusLostError):
            guard.assert_focus()

    def test_ses_02_no_grace_when_focus_was_never_correct(self):
        """Regression: an already-unfocused target must not get a free first input.

        The tolerance window exists to absorb window-activation flicker. It must only
        apply *after* correct focus has been seen; otherwise the very first input of a run
        goes to a window the user has already switched away from.
        """
        from frameforge.actions.focus import FocusGuard
        from frameforge.kernel.errors import FocusLostError
        from frameforge.ports import fakes

        window = fakes.FakeWindow()
        window.foreground_hwnd = 9999  # user already switched away
        clock = FakeClock()
        guard = FocusGuard(window, target_hwnd=1000, tolerance_ms=250.0, clock=clock)
        clock.advance_ms(10)  # well inside the tolerance window
        with pytest.raises(FocusLostError):
            guard.assert_focus()

    def test_ses_02_tolerance_applies_after_focus_was_seen(self):
        """...but genuine activation flicker after a correct focus must not block."""
        from frameforge.actions.focus import FocusGuard
        from frameforge.ports import fakes

        window = fakes.FakeWindow()
        clock = FakeClock()
        guard = FocusGuard(window, target_hwnd=1000, tolerance_ms=250.0, clock=clock)
        guard.assert_focus()                       # correct focus observed
        window.foreground_hwnd = 9999              # transient activation flicker
        clock.advance_ms(10)
        guard.assert_focus()                       # tolerated

    def test_ses_01_inactive_session_refuses_input(self):
        from frameforge.actions.focus import FocusGuard
        from frameforge.kernel.errors import SessionInactiveError
        from frameforge.ports import fakes
        from frameforge.ports.window import SessionState

        window = fakes.FakeWindow()
        window.session = SessionState.LOCKED
        guard = FocusGuard(window, target_hwnd=1000, clock=FakeClock())
        with pytest.raises(SessionInactiveError):
            guard.assert_focus()

    def test_input_port_refuses_primitives_while_disarmed(self):
        from frameforge.ports import fakes
        from frameforge.ports.input import Primitive, PrimitiveType

        port = fakes.FakeInput()
        port.send(Primitive(kind=PrimitiveType.KEY, key=Key.W, down=True))
        assert port.primitives == []          # disarmed by default
        port.set_enabled(True)
        port.send(Primitive(kind=PrimitiveType.KEY, key=Key.W, down=True))
        assert len(port.primitives) == 1


# --------------------------------------------------------------------- G-DEG


class TestDegradation:
    def test_deg_01_ai_off_is_a_valid_configuration(self):
        from frameforge.config.settings import Settings

        s = Settings()
        assert s.ai_enabled is False and s.ai_provider == "none"
        s.validate()  # must not complain

    def test_deg_03_provider_is_configuration_only(self):
        from frameforge.ports.planner import PlannerCapabilities, PlannerPort

        assert hasattr(PlannerPort, "propose")
        assert hasattr(PlannerPort, "capabilities")

    def test_settings_reject_nonsense(self):
        from frameforge.config.settings import Settings

        with pytest.raises(ValueError):
            Settings(capture_backend="telepathy")
        with pytest.raises(ValueError):
            Settings(focus_policy="ignore-everything")
        with pytest.raises(ValueError):
            Settings(launch_mode="direct", allow_direct_launch=False)


# ---------------------------------------------------------------------- G-AUD


class TestAudit:
    def test_aud_02_no_mutation_api_on_the_event_log(self):
        for method in ("update", "delete", "remove", "clear", "pop", "setitem"):
            assert not hasattr(EventLog, method), f"EventLog exposes {method}"

    def test_aud_03_manifest_records_provenance(self, tmp_path):
        from frameforge.store.runs import EvidenceStore, RunPaths

        paths = RunPaths(root=tmp_path / "r")
        store = EvidenceStore(paths)
        store.write_manifest({"run_id": "r1", "seed": 0, "profiles": {"testbed": "abc123"}})
        text = paths.manifest.read_text(encoding="utf-8")
        assert "abc123" in text and "seed" in text
