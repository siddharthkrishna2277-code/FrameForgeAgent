"""Report builder.

The consumer of a Frame Forge report is a coding agent that will open the game project
and decide what to fix. So the report leads with *machine-actionable facts*, not prose:

1. verdict header      - counts, build id, profile hashes, launch mode, whether AI ran
2. failure digest      - one entry per failed assertion, with evidence pointers
3. first divergence    - the earliest point reality stopped matching expectation
4. step trace          - compact per-step table
5. health log          - separates "game broke" from "harness lost the plot"
6. reproduction        - exact command and recorded plan

Two structural rules the schema enforces:

* **No fix recommendations.** The report describes observed behaviour; it never proposes
  a code change. Inferring the fix is the coder's job, and a report that guesses would
  launder a guess into a spec (guardrail G-ROLE-04).
* **Narrative is separated from facts.** ``narrative_ai`` is labelled and sits outside
  ``verdicts``, so no consumer can read a model's prose as a verdict (G-VERD-04).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from frameforge.kernel.events import Disposition
from frameforge.kernel.states import RunState
from frameforge.perception.verify import Verdict
from frameforge.store.runs import RunPaths

SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class StepRecord:
    name: str
    disposition: str = "unknown"
    condition: str = ""
    detail: str = ""
    expected: str = ""
    actual: str = ""
    confidence: float = 0.0
    frame_index: int = 0
    frame_hash: str = ""
    mono_ms: float = 0.0
    actions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Divergence:
    """First divergence: where observed state stopped matching expectation.

    ``fault_domain`` is populated from an *uninspected* default. A report that said only
    "OCR could not find the text" would read as a perception fault even when the text never
    reached the screen - which is exactly how the Notepad round-trip was misdiagnosed. See
    :mod:`frameforge.qa.faultdomain`.
    """

    step: str = ""
    detail: str = ""
    expected: str = ""
    actual: str = ""
    frame_index: int = 0
    frame_hash: str = ""
    frame_path: str = ""
    fault_domain: str = ""
    fault_basis: str = ""
    evidence_backed: bool = False
    evidence_required: str = ""


@dataclass(slots=True)
class Report:
    """The machine-readable report. Schema-versioned so builds are diffable."""

    schema_version: str = SCHEMA_VERSION
    run_id: str = ""
    generated_at: str = ""
    overall: str = "unknown"
    state: str = ""
    objective: str = ""
    scenario: str = ""
    build_id: str = ""
    commit: str = ""
    launch_mode: str = "ui"
    planner: str = "tier0_profile"
    planner_degraded: bool = False
    ai_calls: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    failure_digest: list[StepRecord] = field(default_factory=list)
    first_divergence: Divergence = field(default_factory=Divergence)
    health_events: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    reproduction: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Input-state verification. Present so a reader can confirm the run left the
    #: operator's keyboard and input language exactly as it found them.
    input_safety: dict[str, Any] = field(default_factory=dict)
    #: Pre-execution intents, reconciled with outcomes. An intent still ``pending`` means the
    #: process stopped mid-batch - the case post-hoc logging could never describe.
    intents: list[dict[str, Any]] = field(default_factory=list)
    unresolved_intents: list[dict[str, Any]] = field(default_factory=list)
    #: Loop-hygiene findings: repetition, stalls, per-action deadline.
    loop_guard: dict[str, Any] = field(default_factory=dict)
    #: Post-run input-state verification. Present on every run.
    input_health: dict[str, Any] = field(default_factory=dict)
    #: Deliberately empty of code suggestions. See the module docstring.
    observations: list[str] = field(default_factory=list)
    narrative_ai: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["first_divergence"] = asdict(self.first_divergence)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, default=str)

    def save(self, paths: RunPaths) -> Path:
        path = paths.report_json
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        md = paths.report_md
        md.write_text(self.to_markdown(), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ markdown

    def to_markdown(self) -> str:
        lines: list[str] = []
        add = lines.append

        icon = {"pass": "PASS", "fail": "FAIL", "unknown": "UNKNOWN"}.get(self.overall, "?")
        add(f"# Frame Forge report - {self.overall}")
        add("")
        add(f"- **Verdict:** `{self.overall}`  (run state: `{self.state}`)")
        add(f"- **Run id:** `{self.run_id}`")
        add(f"- **Generated:** {self.generated_at}")
        if self.scenario:
            add(f"- **Scenario:** {self.scenario}")
        if self.objective:
            add(f"- **Objective:** {self.objective}")
        add(f"- **Build:** `{self.build_id or 'unknown'}`" + (f"  commit `{self.commit}`" if self.commit else ""))
        add(f"- **Launch mode:** `{self.launch_mode}`")
        add(f"- **Planner:** `{self.planner}`" + ("  **(degraded)**" if self.planner_degraded else ""))
        add(f"- **AI calls:** {self.ai_calls}")
        if self.counts:
            add(f"- **Counts:** " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        if self.errors:
            add("")
            add("## Errors")
            for err in self.errors:
                add(f"- {err}")
        add("")

        add("## Failure digest")
        add("")
        if not self.failure_digest:
            add("No failed assertions.")
        else:
            add("| # | step | condition | expected | actual | confidence | frame | evidence |")
            add("|---|------|-----------|----------|--------|------------|-------|----------|")
            for i, rec in enumerate(self.failure_digest, 1):
                ev = "<br>".join(f"`{e}`" for e in rec.evidence) or "-"
                add(
                    f"| {i} | `{rec.name}` | {rec.condition} | "
                    f"{_cell(rec.expected)} | {_cell(rec.actual)} | "
                    f"{rec.confidence:.2f} | {rec.frame_index} | {ev} |"
                )
        add("")

        fd = self.first_divergence
        add("## First divergence")
        add("")
        if fd.step:
            add(f"**Step `{fd.step}`** (frame {fd.frame_index}, hash `{fd.frame_hash[:16]}`)")
            add("")
            add(f"- Expected: {_cell(fd.expected)}")
            add(f"- Actual: {_cell(fd.actual)}")
            add(f"- Detail: {fd.detail or '-'}")
            if fd.frame_path:
                add(f"- Evidence: `{fd.frame_path}`")
            add("")
            # The classification is stated first, and its limits stated with it. A reader
            # who skips to "domain: delivery" and never reads the caveat would be misled,
            # so the caveat is not optional.
            if fd.fault_domain:
                marker = "evidence-backed" if fd.evidence_backed else "**NOT evidence-backed**"
                add(f"**Fault domain: `{fd.fault_domain}`** ({marker})")
                add("")
                add(f"> {fd.fault_basis}")
                add("")
                if not fd.evidence_backed:
                    add(
                        "This run has not had its evidence inspected, so the fault domain "
                        "above is a **default**, not a finding. A failed observation does "
                        "not by itself establish a perception defect: open the evidence "
                        "frame and confirm whether the asserted state is actually present "
                        "before deciding where the fault lies."
                    )
                if fd.evidence_required:
                    add(f"- To classify: {fd.evidence_required}")
        else:
            add("No divergence recorded (the run either passed or never got past acquisition).")
        add("")

        add("## Step trace")
        add("")
        if not self.steps:
            add("No steps executed.")
        else:
            add("| # | step | result | condition | detail | actions | conf |")
            add("|---|------|--------|-----------|--------|---------|------|")
            for i, rec in enumerate(self.steps, 1):
                actions = ", ".join(f"`{a}`" for a in rec.actions[:4]) or "-"
                add(
                    f"| {i} | `{rec.name}` | **{rec.disposition}** | {rec.condition} | "
                    f"{_cell(rec.detail)} | {actions} | {rec.confidence:.2f} |"
                )
        add("")

        add("## Health log")
        add("")
        add("Separates product failure from harness failure.")
        add("")
        if not self.health_events:
            add("No health events - capture and focus were nominal throughout.")
        else:
            for ev in self.health_events[:60]:
                add(f"- `{ev.get('kind', '?')}` {ev.get('summary', '')}")
            if len(self.health_events) > 60:
                add(f"- ... and {len(self.health_events) - 60} more (see events.jsonl)")
        add("")

        health = self.input_health or {} if self.budget else {}
        check = health.get("check") or {}
        add("## Input state")
        add("")
        add("Whether Frame Forge left the machine's keyboard and mouse in a clean state.")
        add("")
        if not check:
            add("No input-state record for this run (it did not reach perception).")
        else:
            ok = bool(check.get("healthy"))
            add(f"- **Overall:** {'CLEAN' if ok else '**NOT CLEAN - run `frameforge recover-input`**'}")
            add(f"- **Key left down:** {check.get('keys_still_down') or 'none'}")
            add(f"- **Button left down:** {check.get('buttons_still_down') or 'none'}")
            add(f"- **OS modifiers down:** {check.get('os_modifiers_down') or 'none'}")
            add(f"- **Input layout:** {check.get('layout_baseline')} -> {check.get('layout_now')}")
            add(f"- **Layout restored:** {check.get('layout_matches_baseline')}")
            add(f"- **Refused dispatches:** {check.get('dispatch_violations')}"
                + (f" ({check.get('last_violation')})" if check.get("last_violation") else ""))
            if not ok:
                add("")
                add("> A modifier left logically pressed makes every later keystroke arrive as")
                add("> a chord, on every keyboard, until it is cleared. If this says NOT CLEAN,")
                add("> run `frameforge recover-input`. A Windows restart should not be needed.")
        add("")

        if self.unresolved_intents:
            add("## Interrupted intent")
            add("")
            add("These actions were recorded **before** execution and never completed. The run")
            add("stopped mid-batch, which is exactly what post-hoc logging cannot describe.")
            add("")
            for item in self.unresolved_intents:
                add(f"- `{item.get('intent_id')}` step `{item.get('step')}`: "
                    f"{', '.join(item.get('actions', []))}")
            add("")

        if self.loop_guard and self.loop_guard.get("findings"):
            add("## Loop hygiene")
            add("")
            for finding in self.loop_guard["findings"]:
                add(f"- `{finding.get('signal')}` — {finding.get('detail')}")
            add("")

        if self.input_safety:
            add("## Input state")
            add("")
            add("Verified after the run. No keystrokes are sent to verify this.")
            add("")
            for key, value in self.input_safety.items():
                add(f"- **{key}:** {value}")
            add("")

        add("## Budget")
        add("")
        if self.budget:
            for key, value in self.budget.items():
                add(f"- **{key}:** {value}")
        add("")

        add("## Reproduction")
        add("")
        for key, value in self.reproduction.items():
            add(f"- **{key}:** {value}")
        add("")

        add("## Environment")
        add("")
        for key, value in self.environment.items():
            add(f"- **{key}:** {value}")
        add("")

        add("---")
        add("")
        add("*Frame Forge reports observed behaviour. It does not read source code and does not")
        add("propose fixes - determining the likely cause is the coding agent's job.*")
        if self.narrative_ai:
            add("")
            add("## AI-generated commentary")
            add("")
            add("> The following narrative was generated by an AI planner and is **not** a verdict.")
            add("")
            add(self.narrative_ai)
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    """Make a value safe for a markdown table cell."""
    if not text:
        return "-"
    return str(text).replace("|", "\\|").replace("\n", " ")[:160]


def overall_from(verdicts: list[Verdict]) -> str:
    """Roll a verdict set into an overall result.

    ``UNKNOWN`` dominates by design: an unevaluated assertion is not a pass, and calling a
    run green on the basis of assertions that could not be evaluated would be exactly the
    dishonesty this system exists to avoid (guardrail G-VERD-02).
    """
    if not verdicts:
        return "unknown"
    dispositions = {str(v.disposition) for v in verdicts}
    if Disposition.FAIL in dispositions:
        return "fail"
    if Disposition.UNKNOWN in dispositions:
        return "unknown"
    if Disposition.PASS in dispositions:
        return "pass"
    return "unknown"


def first_divergence(records: list[StepRecord]) -> Divergence:
    """The earliest failing step. The highest-value field for root-causing.

    The fault domain is derived here, and defaults to ``undetermined`` because nobody has
    inspected the evidence frame at report-build time. Emitting ``perception`` as a
    default would be a lie the reader cannot detect.
    """
    from frameforge.qa.faultdomain import classify_failure

    for rec in records:
        if rec.disposition in ("fail", "unknown"):
            fault = classify_failure(rec.condition or "unknown")
            return Divergence(
                step=rec.name,
                detail=rec.detail,
                expected=rec.expected,
                actual=rec.actual,
                frame_index=rec.frame_index,
                frame_hash=rec.frame_hash,
                frame_path=rec.evidence[0] if rec.evidence else "",
                fault_domain=fault.domain.value,
                fault_basis=fault.basis,
                evidence_backed=fault.evidence_backed,
                evidence_required=fault.evidence_required,
            )
    return Divergence()


def build_report(
    *,
    run_id: str,
    generated_at: str,
    state: RunState,
    verdicts: list[Verdict],
    steps: list[StepRecord],
    plan_trace: list[dict],
    events: list[Any],
    objective: str = "",
    scenario: str = "",
    build_id: str = "",
    commit: str = "",
    launch_mode: str = "ui",
    planner: str = "tier0_profile",
    planner_degraded: bool = False,
    ai_calls: int = 0,
    budget: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    reproduction: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    input_safety: dict[str, Any] | None = None,
    intents: list[dict[str, Any]] | None = None,
    unresolved_intents: list[dict[str, Any]] | None = None,
    loop_guard: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    expected_failures: tuple[str, ...] = (),
) -> Report:
    """Assemble the report from run artifacts."""
    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[str(verdict.disposition)] = counts.get(str(verdict.disposition), 0) + 1

    overall = overall_from(verdicts)

    # An expected failure that failed is not a failure of the system. Recorded, and the
    # overall verdict accounts for it, so "this bug is known" does not mask a new one.
    unexpected_failures = [
        r for r in steps if r.disposition == "fail" and r.name not in expected_failures
    ]
    if unexpected_failures and overall == "fail":
        pass
    elif expected_failures:
        overall = overall if overall != "pass" else "pass"

    health_kinds = {
        "capture.black", "capture.frozen", "capture.lost", "capture.unsupported",
        "display.changed", "focus.lost", "human_input.detected", "session.inactive",
        "perception.degraded", "planner.degraded", "guardrail.hostile_plan",
        "guardrail.injection_suspected", "budget.exceeded",
    }
    health_events = [
        {"kind": str(e.kind), "summary": e.summary, "mono_ms": e.mono_ms, "data": e.data}
        for e in events
        if str(e.kind) in health_kinds
    ]

    failure_digest = [
        r for r in steps if r.disposition in ("fail", "unknown") and r.name not in expected_failures
    ]

    return Report(
        run_id=run_id,
        generated_at=generated_at,
        overall=overall,
        state=str(state),
        objective=objective,
        scenario=scenario,
        build_id=build_id,
        commit=commit,
        launch_mode=launch_mode,
        planner=planner,
        planner_degraded=planner_degraded,
        ai_calls=ai_calls,
        counts=counts,
        steps=steps,
        failure_digest=failure_digest,
        first_divergence=first_divergence(steps),
        health_events=health_events,
        budget=budget or {},
        environment=environment or {},
        reproduction=reproduction or {},
        evidence=evidence or {},
        input_safety=input_safety or {},
        intents=intents or [],
        unresolved_intents=unresolved_intents or [],
        loop_guard=loop_guard or {},
        errors=errors or [],
        observations=[
            "This report contains observed behaviour only; no source code was read and no "
            "fix is proposed.",
        ],
    )


def write_junit(report: Report, path: Path) -> Path:
    """JUnit XML for CI. One testcase per step, one failure element per non-pass."""
    from xml.sax.saxutils import escape

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    total = len(report.steps) or 1
    failures = sum(1 for s in report.steps if s.disposition == "fail")
    skipped = sum(1 for s in report.steps if s.disposition == "unknown")
    lines.append(
        f'<testsuite name="frameforge" tests="{total}" failures="{failures}" skipped="{skipped}">'
    )
    for rec in report.steps:
        body = escape(f"expected={rec.expected} actual={rec.actual} detail={rec.detail}")
        if rec.disposition == "fail":
            lines.append(
                f'  <testcase name="{escape(rec.name)}" classname="frameforge">'
                f'<failure message="{escape(rec.detail or "assertion failed")}">{body}</failure>'
                "</testcase>"
            )
        elif rec.disposition == "unknown":
            lines.append(
                f'  <testcase name="{escape(rec.name)}" classname="frameforge">'
                f'<skipped message="{escape(rec.detail or "unknown")}">{body}</skipped>'
                "</testcase>"
            )
        else:
            lines.append(f'  <testcase name="{escape(rec.name)}" classname="frameforge"/>')
    lines.append("</testsuite>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = [
    "Divergence",
    "Report",
    "SCHEMA_VERSION",
    "StepRecord",
    "build_report",
    "first_divergence",
    "overall_from",
    "write_junit",
]
