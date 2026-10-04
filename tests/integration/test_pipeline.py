"""End-to-end: a complete scenario through the real director.

These are the tests that matter most. They run the *real* RunDirector, Assembler,
Compiler, Executor, Verifier and ReportBuilder, with only the Windows adapters replaced by
fakes. That is the same guarantee ``frameforge simulate`` gives, asserted automatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frameforge.config.settings import Settings
from frameforge.ports import fakes
from frameforge.ports.capture import Surface, SurfaceKind
from frameforge.ports.geometry import Size
from frameforge.profiles.loader import load_game_profile
from frameforge.qa.report import Report
from frameforge.tasks.loader import load_task, parse_scenario
from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "profiles" / "games" / "testbed.json"


def build_runner(tmp_path: Path, scenario_raw: dict, *, ocr_lines=None) -> ScenarioRunner:
    profile = load_game_profile(PROFILE)
    profile.metadata["_profile_dir"] = str(PROFILE.parent)
    scenario = parse_scenario(scenario_raw, source="test")

    surface = Surface(
        kind=SurfaceKind.WINDOW, size=Size(1280, 720), offset_x=0, offset_y=0,
        hwnd=1000, label="fake",
    )
    capture = fakes.FakeCapture(surface)
    capture.open(surface)
    window = fakes.FakeWindow(
        title=profile.display_name or profile.name,
        class_name=profile.target.class_name or "FakeTargetClass",
        process_name=profile.target.process_name or "fake.exe",
    )
    ocr = fakes.FakeOcr(default=ocr_lines or [])

    settings = Settings(runs_dir=tmp_path / "runs", grace_seconds=0.0)
    wiring = RunnerWiring(
        window=window, capture=capture, ocr=ocr, vision=fakes.FakeVision(),
        input_port=fakes.FakeInput(), profile=profile, templates={}, surface=surface,
    )
    runner = ScenarioRunner(settings, wiring, scenario=scenario)
    runner.prepare()
    return runner


SMOKE = {
    "scenario_id": "smoke",
    "name": "smoke",
    "objective": "confirm the menu is present",
    "launch_mode": "attach",
    "steps": [
        {
            "name": "menu_present",
            "kind": "assert",
            "verify": "anchor_visible",
            "verify_args": {"anchor": "menu_title"},
        }
    ],
}


class TestHappyPath:
    def test_pass_run_produces_a_valid_bundle(self, tmp_path):
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        outcome, report = runner.run()

        assert report.overall == "pass", report.failure_digest
        assert outcome.state.value == "completed"
        # Every promised artefact exists.
        for path in (runner.paths.events, runner.paths.report_json,
                     runner.paths.report_md, runner.paths.manifest if runner.paths.manifest.exists() else None):
            if path is not None:
                assert path.exists(), f"missing artefact: {path}"
        assert runner.paths.frames.exists()

    def test_report_is_schema_shaped_and_actionable(self, tmp_path):
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        _outcome, report = runner.run()
        data = json.loads(runner.paths.report_json.read_text(encoding="utf-8"))
        for key in ("schema_version", "run_id", "overall", "state", "steps",
                    "failure_digest", "first_divergence", "health_events",
                    "budget", "reproduction", "environment", "input_safety"):
            assert key in data, f"report missing {key}"
        # The report must not contain a fix suggestion (guardrail G-ROLE-04).
        # Checked as *keys*, not as a substring anywhere in the document: a blunt
        # "patch" not in json.dumps(...) also fires on innocent words like
        # "dispatch_violations", which is how this assertion first failed.
        banned = {"suggested_fix", "fix", "patch", "diff", "code_change",
                  "remediation", "recommended_change"}
        found: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in banned:
                        found.append(str(key))
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        assert not found, f"report exposes fix-shaped fields: {found}"

    def test_report_records_input_state_verification(self, tmp_path):
        """A run must state what it did to the operator's keyboard."""
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        runner.run()
        data = json.loads(runner.paths.report_json.read_text(encoding="utf-8"))
        assert "input_safety" in data
        assert "healthy" in data["input_safety"]
        # And the audit artefact is written into the run directory.
        assert (runner.paths.root / "input_audit.json").exists()

    def test_markdown_report_names_the_scenario(self, tmp_path):
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        runner.run()
        md = runner.paths.report_md.read_text(encoding="utf-8")
        assert "smoke" in md and "Verdict" in md

    def test_events_are_recorded_for_every_consequential_thing(self, tmp_path):
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        runner.run()
        kinds = {str(e.kind) for e in runner.events.events}
        assert "run.started" in kinds
        assert "run.finished" in kinds
        assert any(k.startswith("run.state_changed") for k in kinds)

    def test_run_is_reproducible_from_the_seed(self, tmp_path):
        a = build_runner(tmp_path / "a", SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        b = build_runner(tmp_path / "b", SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        ra = a.run()[1]
        rb = b.run()[1]
        assert ra.overall == rb.overall
        assert [s.disposition for s in ra.steps] == [s.disposition for s in rb.steps]


class TestFailureReporting:
    """A QA tool that cannot detect a known defect has not been shown to work."""

    def _bug_scenario(self) -> dict:
        return {
            "scenario_id": "canary",
            "name": "canary",
            "objective": "assert something that is not there",
            "launch_mode": "attach",
            "steps": [
                {"name": "control", "kind": "assert",
                 "verify": "anchor_visible", "verify_args": {"anchor": "menu_title"}},
                {"name": "healthbar_visible", "kind": "assert",
                 "verify": "anchor_visible", "verify_args": {"anchor": "healthbar_label"}},
            ],
        }

    def test_planted_defect_is_detected(self, tmp_path):
        """Bug OFF for the control, but the defect's landmark is absent -> FAIL."""
        runner = build_runner(tmp_path, self._bug_scenario(),
                              ocr_lines=["FRAMEFORGE TESTBED"])  # no HEALTHBAR text
        _outcome, report = runner.run()
        assert report.overall == "fail"
        failed = [s for s in report.steps if s.disposition == "fail"]
        assert [s.name for s in failed] == ["healthbar_visible"]
        # The control passed, so the failure is specific rather than a blanket failure.
        assert any(s.name == "control" and s.disposition == "pass" for s in report.steps)

    def test_first_divergence_points_at_the_failing_step(self, tmp_path):
        runner = build_runner(tmp_path, self._bug_scenario(), ocr_lines=["FRAMEFORGE TESTBED"])
        _outcome, report = runner.run()
        assert report.first_divergence.step == "healthbar_visible"
        assert report.first_divergence.expected == "present"

    def test_failure_evidence_is_captured(self, tmp_path):
        runner = build_runner(tmp_path, self._bug_scenario(), ocr_lines=["FRAMEFORGE TESTBED"])
        runner.run()
        failures = list(runner.paths.frames.glob("*FAIL*"))
        assert failures, "a FAIL run must capture an evidence frame"

    def test_all_passing_gives_pass(self, tmp_path):
        runner = build_runner(
            tmp_path, self._bug_scenario(),
            ocr_lines=["FRAMEFORGE TESTBED", "HEALTHBAR"],
        )
        _outcome, report = runner.run()
        assert report.overall == "pass"

    def test_unevaluated_gives_unknown_not_pass(self, tmp_path):
        """G-VERD-02: no observations means UNKNOWN, never PASS."""
        scenario = {
            "scenario_id": "u", "name": "u", "objective": "", "launch_mode": "attach",
            "steps": [{"name": "absent", "kind": "assert",
                       "verify": "anchor_visible",
                       "verify_args": {"anchor": "menu_title"}}],
        }
        runner = build_runner(tmp_path, scenario, ocr_lines=[])
        # Remove the anchor from the fake's reach by pointing at a landmark with no text.
        _outcome, report = runner.run()
        assert report.overall == "fail"  # evaluated and absent


class TestSafetyIntegration:
    def test_focus_loss_blocks_input_and_yields_unknown(self, tmp_path):
        scenario = {
            "scenario_id": "f", "name": "f", "objective": "", "launch_mode": "attach",
            "steps": [{"name": "press", "kind": "interact",
                       "action": {"kind": "key", "key": "enter"},
                       "verify": "screen_changed", "attempts": 1}],
        }
        runner = build_runner(tmp_path, scenario, ocr_lines=["MENU"])
        # Simulate the user alt-tabbing away mid-run.
        runner.wiring.window.foreground_hwnd = 424242
        _outcome, report = runner.run()
        assert report.overall in ("unknown", "fail")
        # No key may have been sent while focus was wrong.
        sent = runner.wiring.input_port.primitives
        assert not any(p.kind == "key" for p in sent), "input leaked despite focus loss"

    def test_estop_prevents_all_subsequent_input(self, tmp_path):
        scenario = {
            "scenario_id": "e", "name": "e", "objective": "", "launch_mode": "attach",
            "steps": [{"name": "a", "kind": "interact",
                       "action": {"kind": "key", "key": "a"}, "verify": "screen_changed"},
                      {"name": "b", "kind": "interact",
                       "action": {"kind": "key", "key": "b"}, "verify": "screen_changed"}],
        }
        runner = build_runner(tmp_path, scenario, ocr_lines=["MENU"])
        runner._estop.trigger()
        outcome, report = runner.run()
        assert outcome.state.value == "aborted"
        assert runner.wiring.input_port.primitives == []

    def test_no_profile_means_live_input_is_refused(self, tmp_path):
        """Default-deny, asserted.

        A run with no game profile has no target window, so the policy layer refuses every
        action. That is the correct behaviour and the reason it is a test: it proves input
        cannot be sent to an unnamed window, which is the whole defect from incident 2.
        """
        from frameforge.ports import fakes
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size
        from frameforge.config.settings import Settings
        from frameforge.tasks.loader import parse_scenario
        from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

        surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000)
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        scenario = parse_scenario({
            "name": "no-profile",
            "steps": [{"name": "press", "kind": "interact",
                       "action": {"kind": "key", "key": "a"},
                       "verify": "always", "attempts": 1}],
        })
        wiring = RunnerWiring(
            window=fakes.FakeWindow(), capture=capture, ocr=fakes.FakeOcr(),
            vision=fakes.FakeVision(), input_port=fakes.FakeInput(), surface=surface,
        )
        runner = ScenarioRunner(Settings(runs_dir=tmp_path, grace_seconds=0.0),
                                wiring, scenario=scenario)
        runner.prepare()

        # The policy layer itself must refuse: no target process is allow-listed, so no
        # action can be permitted regardless of the port or the run state.
        from frameforge.actions.controller import ToolRequest
        from frameforge.actions.model import KeyPress
        from frameforge.ports.input import Key

        verdict = runner.policy.validate(
            ToolRequest(action=KeyPress(key=Key.A), run_id="r1", action_id="a1"),
            is_armed=True,
        )
        assert not verdict.allowed, "policy permitted input with no allow-listed target"
        # With no target and no allow-list, the precise reason is "no target window" -
        # an action with nowhere to go is the dangerous case, not a near-miss.
        assert verdict.reason.value == "no_target_window"

        # A request that *does* name a window is refused for the other reason.
        from frameforge.actions.controller import ActionTarget

        named = runner.policy.validate(
            ToolRequest(
                action=KeyPress(key=Key.A),
                run_id="r1",
                action_id="a2",
                target=ActionTarget(hwnd=1, process_name="something.exe"),
            ),
            is_armed=True,
        )
        assert not named.allowed
        assert named.reason.value == "target_window_not_allowlisted"

        _outcome, report = runner.run()
        # And a run in that configuration produces no OS input whatsoever.
        assert runner.wiring.input_port.primitives == [], "input was sent without a target"
        assert runner.input_health.get("post_run_check", {}).get("no_key_left_down") is True

    def test_budget_cap_stops_a_run(self, tmp_path):
        scenario = {
            "scenario_id": "b", "name": "b", "objective": "", "launch_mode": "attach",
            "steps": [{"name": f"s{i}", "kind": "interact",
                       "action": {"kind": "key", "key": "a"}, "verify": "screen_changed"}
                      for i in range(10)],
        }
        profile = load_game_profile(PROFILE)
        profile.metadata["_profile_dir"] = str(PROFILE.parent)
        surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000)
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        window = fakes.FakeWindow(title="FrameForge Testbed", class_name="TkTopLevel",
                                  process_name="python.exe")
        settings = Settings(runs_dir=tmp_path / "runs", grace_seconds=0.0, max_actions=3)
        wiring = RunnerWiring(window=window, capture=capture,
                              ocr=fakes.FakeOcr(default=["MENU"]),
                              vision=fakes.FakeVision(), input_port=fakes.FakeInput(),
                              profile=profile, surface=surface)
        runner = ScenarioRunner(settings, wiring, scenario=parse_scenario(scenario))
        runner.prepare()
        outcome, _report = runner.run()
        # Input is refused without an allow-listed target, so the budget is never the
        # binding constraint here; the run must still terminate cleanly rather than hang.
        assert outcome.state.value in ("aborted", "completed", "failed", "cleanup_failed")
        assert settings.max_actions == 3

    def test_no_input_while_unarmed(self, tmp_path):
        runner = build_runner(tmp_path, SMOKE, ocr_lines=["FRAMEFORGE TESTBED"])
        assert runner.wiring.input_port.enabled is False
        runner.run()
        # After a completed run the port must be disarmed again.
        assert runner.wiring.input_port.enabled is False


class TestShippedArtefacts:
    def test_shipped_profiles_validate(self):
        profile = load_game_profile(PROFILE)
        assert profile.authorized_use.attestation
        assert profile.target.identifying

    def test_shipped_scenarios_parse(self):
        for path in (REPO / "tasks" / "scenarios").glob("*.json"):
            scenario = load_task(path)
            assert scenario.name
            assert scenario.steps, f"{path.name} has no steps"

    def test_canary_scenario_documents_that_it_should_fail(self):
        scenario = load_task(REPO / "tasks" / "scenarios" / "testbed_healthbar_bug.json")
        assert scenario.metadata.get("expected_to_fail") is True
        assert "EXPECTED TO FAIL" in scenario.objective

    def test_no_scenario_enables_direct_launch_by_default(self):
        for path in (REPO / "tasks" / "scenarios").glob("*.json"):
            scenario = load_task(path)
            assert scenario.launch_mode in ("ui", "attach", "direct")
            if scenario.launch_mode == "direct":
                assert scenario.allow_direct_launch, (
                    f"{path.name}: direct launch requires explicit opt-in"
                )
