# Frame Forge — AI Guardrails

Version: 1.0 (pre-implementation baseline)
Status: **binding on all implementation work from P0 onward**
Scope: (A) the runtime AI planner and any future learned/derived component;
(B) any AI coding agent (including Hermes/Herman/Cline) writing code in this repository.
Owner: lead architect/implementation agent. Deviations require a written ADR + owner sign-off.

---

## 0. Purpose and the one-sentence version

Frame Forge may **reason** about a game and propose *what should happen next*.
It may **never** decide whether a test passed, decide that it is allowed to do
something, emit an input primitive, modify game or system state outside its sandbox,
or touch a game it was not explicitly authorized to touch.

> **The AI is a proposal generator inside a policy envelope. The envelope is code,
> not the model, not the prompt, and not the operator.**

Two failure modes this document exists to prevent:
1. **Capability failure** — the AI does something the user never authorised.
2. **Epistemic failure** — the AI asserts a confident falsehood (a wrong QA verdict)
   and the product launders that falsehood into a "verified" report.

The second is the more dangerous one for a QA tool, and it is the one most naive
"LLM agent" designs fail.

---

## 1. Enforcement layers

Rules are not advisory prose. Each rule below names **where it is enforced in code**
and **the test that proves it**. The defence is layered so that no single failure opens the system.

```
L0  TASK-TIME POLICY   profile/schema/CLI validation at load.  Fail-closed.
L1  PERCEPTION BOUND   what the AI is even shown. Redaction + minimisation.
L2  PROPOSAL BOUND     PlanValidator: schema, permission, budget, irreversibility.
L3  DECISION BOUND     ConfidenceScorer + FallbackPolicy + ConfidenceGate.
L4  EXECUTION BOUND    FocusGuard + UserInputDetector + Estop + SessionGuard.
L5  VERDICT BOUND      Verifier is deterministic-only. AI has NO write path to a verdict.
L6  AUDIT              Append-only events.jsonl + manifest hashes + budget ledger.
```

**L-LAW — fail-closed.** Any uncertainty, parse failure, timeout, missing capability,
or unknown field in L0/L1/L2 results in *the action not being taken*. Never in
"best-effort execution", never in "execute then validate", never in a permissive default.

**L-ORDER — validate-before-execute.** Validation is structurally upstream of the
executor. The executor's only input is a validated, compiled, budget-checked
`Primitive` list produced by code that the AI cannot reach.

---

## 2. Role boundary (the Frame Forge / Herman-Cline separation, as an enforceable rule)

| Actor | May do | May **never** do |
|---|---|---|
| Frame Forge | operate a running game via visible UI input, observe, collect evidence, report | read/write the game project's source, produce a patch, build the game, edit game config |
| Herman/Cline (coding agent) | read code, diagnose, patch, build, request a run | drive the game directly, fabricate a run's evidence |
| Runtime AI planner | choose among legal logical actions; propose recovery; draft narrative | emit input; write files; set a verdict; change config; change a profile; escalate its own permissions |

- **G-ROLE-01** — No code path in `src/frameforge/` may open a file inside a game project directory for reading or writing. Enforced: a path-sandbox helper used by every filesystem operation + static grep test `TEST-ROLE-01`.
- **G-ROLE-02** — No module under `src/frameforge/` may import `subprocess` for the purpose of *patching*; `subprocess` is permitted only in `adapters/input`, `adapters/window` (process/window queries), and the opt-in direct-launch adapter. Enforced: import-boundary test `TEST-ROLE-02`.
- **G-ROLE-03** — Frame Forge never writes to a game install directory, save directory, registry hive other than its own config key, or any path outside `runs/`, `profiles/` (read-only at runtime), and its own config. Enforced: write-path allowlist assertion in every adapter + `TEST-ROLE-03`.
- **G-ROLE-04** — A run's report may not contain a recommended source-code change, a diff, or a code snippet presented as a fix. It reports *observed behaviour*; the coder infers the fix. Enforced: `ReportBuilder` schema forbids such fields + `TEST-ROLE-04`.

---

## 3. Absolute prohibitions (no override, no flag, no "temporary" branch)

- **G-ABS-01 — No anti-cheat interaction of any kind.** No detection, no evasion, no
  hooking of anti-cheat or integrity modules, no reading/injecting to protected memory,
  no driver-level input to defeat verification, no stealth, no "user-mode only" variants.
  This is unconditional. There is no configuration, environment variable, or profile flag
  that enables a related capability.
- **G-ABS-02 — No online/multiplayer automation.** No public competitive play, no
  matchmaking, no queueing, no account or progression farming, no ranking manipulation.
- **G-ABS-03 — No protection evasion, obfuscation, or anti-detection of the automation
  itself** (no hiding the cursor, no randomising timings specifically to defeat
  behavioural detection, no "human-like jitter to avoid bans"). Human-like input is
  justified *only* by game mechanics (e.g. a mouse-look curve), never by evasion intent.
- **G-ABS-04 — No credential, payment, or personal-data entry.** The input layer may
  type only into fields a scenario explicitly declares as test fields, and never into a
  field typed `password`, `card`, `cvv`, or matching a payment/autofill heuristic.
  Enforced: input-channel field classifier + `TEST-ABS-04`.
- **G-ABS-05 — No destructive host operations.** No disk formatting, no registry mass
  deletion, no process killing outside the declared target process tree, no power/thermal
  changes, no disabling of security software.
- **G-ABS-06 — No self-modification.** The runtime may not write code, edit profiles,
  edit its own config, or install packages. No `eval`/`exec` of model output, ever, for
  any purpose. Enforced: static ban on `eval`/`exec`/`pickle.loads` of model text + `TEST-ABS-06`.
- **G-ABS-07 — No unbounded autonomy.** No loop may run without a budget from
  `BudgetLedger`. There is no code path that can retry indefinitely.
- **G-ABS-08 — No unattended operation on a session the user is actively using.**
  A detected human input event pauses the run (G-SES-03).

---

## 4. Authorisation model

Every profile is an **authorisation boundary**, not a convenience file.

- **G-AUTH-01** — Every `GameProfile` must carry a non-empty `authorized_use` block naming
  the basis of use, asserted by the owner. Missing or empty ⇒ profile is **rejected at
  load** (L0, fail-closed). No default, no "unknown ⇒ private".
- **G-AUTH-02** — `authorized_use` must be one of: `owner_prototype`, `developer_authorized_qa`,
  `offline_single_player`, `private_sandbox`, `supervised_accessibility`. Any other value ⇒ rejected.
- **G-AUTH-03** — Profiles carrying `offline_single_player` must assert `offline: true`, and the
  runtime must verify the process has no active network path to a matchmaking/anticheat endpoint
  **only if the owner has enabled `offline_enforcement`**; otherwise it emits a prominent
  `WARN` in the report. (Enforcement is opt-in because reliable offline determination is
  imperfect — the warning is mandatory either way.)
- **G-AUTH-04** — A process denylist (known online/anticheat titles) is checked at launch and
  every epoch while attached. A hit ⇒ immediate `ABORTED` with reason `POLICY_DENYLIST`. Enforced: `TEST-AUTH-04`.
- **G-AUTH-05** — **Attestation is the owner's, not Frame Forge's.** The system records the claim
  and its provenance (who declared it, when, profile hash); it never attempts to verify that a
  claim is truthful, and never presents an unverified claim as verified.
- **G-AUTH-06** — An unauthorised target is a hard refusal, not a warning. There is no
  override path in the CLI, the HTTP API, or the config.

---

## 5. Perception boundary — what the AI is allowed to see (L1)

- **G-PER-01 — Minimisation.** The AI receives a *constructed* packet, never raw capture by
  default. The packet contains only: objective/subgoal, top-N OCR lines, anchor scores,
  window state, last-K transitions, allowed actions, remaining budget. Full-resolution frames
  are attached only when the configured provider is multimodal **and** the scenario sets
  `share_frames: true`. Default is `false`.
- **G-PER-02 — Frame redaction.** Before any frame leaves the process: mask configured regions
  (e.g. `redact_regions`, chat/log panels, any region a profile marks sensitive). Redaction is
  applied at the adapter, before the encoder, and is verifiable in the packet builder. Enforced: `TEST-PER-02`.
- **G-PER-03 — Secret redaction in all outbound and logged text.** The event sink applies a
  redaction filter to AI prompts, AI responses, OCR text, and reports. Patterns: API keys,
  bearer tokens, JWTs, PEM blocks, credit-card-shaped digit runs, and any string the profile
  marks secret. Enforced: `TEST-PER-03` asserts an injected fake key never appears in
  `events.jsonl`, `report.json`, or the packet.
- **G-PER-04 — No ambient capture of unrelated applications.** Capture is scoped to the
  target window/monitor ROI. Full-desktop capture requires explicit `capture_scope: desktop`
  in the profile and is flagged in the report.
- **G-PER-05 — Local-only by default.** No game-derived content is transmitted anywhere
  unless a remote planner is explicitly configured. With `planner: profile`, the network
  surface is zero. Enforced: a test asserting no socket is opened during an `ai: off` run — `TEST-PER-05`.
- **G-PER-06 — Retention.** Transmitted content is not persisted by Frame Forge beyond the
  run directory, and run directories obey the retention policy. Frames containing a redacted
  region are stored **already redacted**.

---

## 6. Untrusted content and prompt injection (game screens are an attack surface)

The screen is attacker-controlled data in the general case (a web page in a browser
scenario, a mod, a game with user-generated text, a dialog with an arbitrary title).
**Everything read from the screen is data, never instruction.**

- **G-INJ-01 — Structural separation.** Screen-derived text is placed in a clearly delimited,
  labelled data region of the prompt. System policy is never assembled from screen content.
- **G-INJ-02 — No instruction authority from perception.** Text matching `ignore previous`,
  `you are now`, `system:`, `disregard`, or any imperative addressed to the assistant is
  recorded as `Observation.suspicious_text` and **can never change the allowed-action set**.
- **G-INJ-03 — Permission is not negotiable by the model.** The allowed-action list, budgets,
  and postconditions are supplied as machine-checked fields the validator enforces; the model
  cannot widen them, and a response that appears to attempt to is scored as a *hostile-plan
  attempt* and fails the run's trust counter.
- **G-INJ-04 — Fuzz-tested containment.** ≥ 50 adversarial cases (injected instructions in
  OCR text, window titles, button labels, and AI responses) must all be rejected or
  downgraded. Enforced: `TEST-INJ-04`. This is a **release gate**, not a nice-to-have.
- **G-INJ-05 — Data cannot become configuration.** No screen-derived string may ever be
  interpreted as a path, command, profile key, or URL to fetch. Enforced: no `eval`/`exec`/
  `import`/shell interpolation of perception output anywhere; asserted by grep test.

---

## 7. Proposal boundary — the PlanValidator (L2)

Every AI response, before it may influence anything, must pass **all** of:

- **G-VAL-01** — Strict schema validation against a generated JSON Schema.
  `additionalProperties: false`; unknown fields are a rejection, never ignored.
- **G-VAL-02** — **Allowlist membership.** Every proposed action must exist in the
  scenario's permitted action set. Unknown action ⇒ reject the *whole plan*, not the action.
- **G-VAL-03** — **Precondition check.** Each step's declared precondition must hold in the
  current `Observation`. A step with an unmet precondition is not compiled for execution.
- **G-VAL-04** — Budget decrement before execution. Cost of the plan is charged to the
  ledger *at validation time*; the executor draws from an already-drained budget.
- **G-VAL-05** — **Irreversibility classification** (see §8) is applied by the validator, and
  irreversible actions require `allow_irreversible: true` in the scenario, else rejection.
- **G-VAL-06** — Postcondition required. A step with no `expected_change` cannot be executed —
  the "act and hope" pattern is structurally unavailable to the model.
- **G-VAL-07** — Plan length bound (`max_plan_steps`, default 5) and total action count bound.
- **G-VAL-08** — A validation failure is a *plan-level* event: it counts toward the run's
  `hostile_plan_attempts` counter; 3 strikes ⇒ the planner is disabled for the remainder of
  the run and the run degrades to Tier 0. This bounds both error and adversarial pressure.
- **G-VAL-09** — Determinism of rejection: the same invalid response must be rejected
  identically every time (no sampling in the validator). Validator is pure.

---

## 8. Blast-radius classification

Every action carries a class. The class caps who may author it and what consent it needs.

| Class | Examples | Authoring | Consent |
|---|---|---|---|
| **C0 observe** | screenshot, wait, look_around, OCR | AI, profile, human | none |
| **C1 reversible-interaction** | click, menu nav, key tap, short hold, scroll | AI, profile, human | scenario |
| **C2 sustained-input** | hold > 2 s, mouse-look, continuous movement, drag | profile, AI (bounded) | scenario |
| **C3 external-effect** | anything that changes state *outside* the game window — clipboard, file save/overwrite, browser navigation to a new origin, network submission, in-game purchase/currency spend, settings that persist | **human-authored profile only** | `allow_irreversible: true` + explicit per-scenario list |
| **C4 forbidden** | anything in §3 | nobody | none |

- **G-BLAST-01** — The AI may author C0–C2 only. C3 is human-authored. Enforced by
  `PlanValidator` acting on the *action* not the *author*: an action in the C3 set is
  rejected regardless of who proposed it. `TEST-BLAST-01`.
- **G-BLAST-02** — C3 actions additionally require a **pre-run dry-run listing** printed in the
  terminal and recorded in the report, so a human sees the external effects before they happen.
- **G-BLAST-03** — C2 durations are hard-capped per step (default 5 s) and per run
  (default 120 s of sustained input). Exceeding ⇒ cut and recover, no extension by the model.
- **G-BLAST-04** — Clipboard access is C3. Reading the clipboard is **forbidden entirely**
  (it is a cross-application data leak and a credential-capture vector). Writing requires C3 consent.

---

## 9. Decision-time guardrails (L3)

- **G-DEC-01 — Confidence gate.** Actions are permitted only in bands (§5 of ROADMAP).
  Below 0.20 the only permitted action is `halt` (or `emergency_stop`).
- **G-DEC-02 — Failure-aware degradation.** The model is told the recent failure history; the
  `FallbackPolicy` decides whether Tier 1 is re-consulted at all after N failures. **A planner
  that caused a failure does not get to immediately retry the same plan** — same-plan
  re-proposal is blocked for the rest of the run unless the observation epoch changed.
- **G-DEC-03 — No self-elevation.** The model cannot request a retry budget increase, a longer
  timeout, a wider action set, a profile switch, or a "skip the verification" flag. There is
  no prompt, flag, or message that grants this. There is deliberately no mechanism.
- **G-DEC-04 — Confidence is not self-reported.** The `ConfidenceScorer` computes the score
  from perception + verification signals. A model's self-reported confidence is recorded as
  an untrusted annotation and **never** feeds the gate. It is useful only for diagnostics.
- **G-DEC-05 — Uncertainty is a valid action.** `UNKNOWN`, `halt`, and `defer_to_human` must be
  available in every action vocabulary. A system that cannot express "I don't know" must not
  be shipped.

---

## 10. Session, focus and attention (L4)

- **G-SES-01** — No input is emitted unless the run is `ARMED` **and** the console session is
  interactive (`WTSGetActiveConsoleSessionId`). A disconnected/locked session receives nothing.
- **G-SES-02** — `FocusGuard` re-verifies foreground identity before **every** input batch.
  Drift beyond tolerance ⇒ inputs cease immediately; the run pauses, re-acquires, and
  re-verifies the last postcondition before resuming.
- **G-SES-03** — `UserInputDetector` samples human input continuously. Default policy on
  detected human input is `PAUSED_USER` and hand-back, not "compete for the mouse".
- **G-SES-04** — A mandatory **5-second grace period** with a visible on-screen countdown
  before the first input of any run. The user can cancel it by touching the machine.
- **G-SES-05** — Screen lock, session switch, or display topology change ⇒ immediate
  `PAUSED`, and any in-flight hold is released.
- **G-SES-06** — **Attention budget.** A run requires positive operator acknowledgement
  (G-SES-04) and the operator may not be warned away mid-run. If the process detecting
  operator absence, the run pauses rather than continuing unattended. *(Post-P0: currently
  "human input detected" is the only absence signal; documented as a known limitation.)*
- **G-SES-07** — Emergency stop is available on three independent paths, always active, never
  requiring AI or a healthy director loop. Full key/button release within 100 ms.

---

## 11. Verdict integrity (L5) — the rule most QA tools get wrong

- **G-VERD-01** — **The AI has no write path to any verdict.** `PASS` / `FAIL` / `UNKNOWN`
  are produced exclusively by the deterministic `Verifier` evaluating a profile-defined
  assertion against a captured `Observation`. Enforced: a static check that no AI-adapter
  module appears in the verdict computation's import graph — `TEST-VERD-01`.
- **G-VERD-02** — An assertion that cannot be evaluated because perception was insufficient
  yields `UNKNOWN`. It may **never** be coerced to `PASS`. There is no "assume passed on
  timeout" path in the codebase.
- **G-VERD-03** — Every verdict is bound to the specific `Observation` (frame hash +
  timestamp) it was derived from. A verdict without an evidence pointer is invalid and
  cannot be serialised.
- **G-VERD-04** — Report text is separated from report facts. AI-authored narrative lives in
  `report.narrative_ai` and is labelled; `report.verdicts` is machine-generated and
  unlabelled. No consumer may read narrative as fact; the schema documents this.
- **G-VERD-05** — No verdict may be cached across runs. Verdicts are per-run and per-build.
- **G-VERD-06** — Absence of a failure is never reported as success of the whole system.
  A scenario with 0 executed assertions yields `UNKNOWN`, not `PASS`.

---

## 12. Audit and non-repudiation (L6)

- **G-AUD-01** — Every consequential event is appended to `events.jsonl` (observe, decide,
  validate, act, verify, health, policy, budget, estop, degradation) with a monotonic
  timestamp, a run id, and the acting component's id.
- **G-AUD-02** — The event log is **append-only**; corrections are new events referencing the
  superseded id. Nothing rewrites history. Enforced: writer API has no update/delete; test asserts it.
- **G-AUD-03** — `manifest.json` records profile hashes, code version, seed, launch mode,
  exact argv, planner id, AI model id, and per-run wall time. A run is reproducible or it is
  honestly labelled as not.
- **G-AUD-04** — Every AI interaction is recorded: the exact packet, the exact response, the
  validation outcome, the cost. Enables offline audit of what the model did.
- **G-AUD-05** — Logs and reports carry no secret (G-PER-03) and are written under
  user-controlled paths only.
- **G-AUD-06** — **The report must be able to say the harness failed.** §D/E of the stability
  criteria require a health log that distinguishes product failure from harness failure, and
  that separation may not be softened in report wording.

---

## 13. Degradation and failure behaviour of the AI tier

- **G-DEG-01** — `ai: off` is a fully supported, first-class, tested mode (ROADMAP
  stability criterion 9). No feature may depend on the AI tier.
- **G-DEG-02** — A configured provider that is unreachable, slow, rate-limited, or returning
  garbage degrades to Tier 0 and records a `PLANNER_DEGRADED` event. It never blocks a run
  and never degrades into a wider action set.
- **G-DEG-03** — Provider swap is configuration. No code change. Providers are behind
  `PlannerPort` and cannot exceed the envelope of any other planner.
- **G-DEG-04** — Cost ceilings: per-run AI call cap, per-run token/cost cap, and a per-minute
  rate cap, all ledger-enforced. Hitting a cap degrades to Tier 0; it never silently truncates
  verification.
- **G-DEG-05** — **No silent substitution.** A degraded run's report says
  `planner_degraded: true` with the reason. A QA consumer must be able to filter those runs.

---

## 14. Development-time guardrails for AI coding agents (including me)

These bind the process of building Frame Forge, and apply to Hermes/Herman/Cline and to any
agent contributing to this repo.

- **G-DEV-01** — No hard-coded game names, screen coordinates, or key bindings in
  `src/frameforge/{kernel,actions,perception,planning}`. Enforced by a test that fails CI
  (ROADMAP criterion 23). Profile data goes in `profiles/`.
- **G-DEV-02** — No new runtime dependency without an ADR recording: why, size, licence, CPU
  cost on the i5-8250U, and whether the system still works with it absent. Extras must be
  optional and degradable (ROADMAP §11).
- **G-DEV-03** — No new capability that reads or writes game project source, produces a patch,
  or builds a game. If a change appears to need that, the requirement is wrong, not the rule.
- **G-DEV-04** — Every safety-critical module (input, estop, focus guard, budget, validator,
  verdict) requires tests that fail when the guard is removed. A guard with no such test is
  not implemented. This is the mutation-testing discipline applied to safety only, because
  that is where a silent regression is most costly.
- **G-DEV-05** — No silent behaviour change. Changes to prompts, schemas, profiles, or
  action semantics bump a version field, and the change is visible in `manifest.json` diffs.
- **G-DEV-06** — No capability may be introduced behind a flag that the safety envelope
  bypasses. "Hidden mode" is prohibited; there is no user-facing or hidden mode.
- **G-DEV-07** — Any rule in this document may be relaxed only by a numbered ADR with an
  explicit threat model, an owner signature, and a note on what an attacker gains. "For
  debugging" is not a threat model.
- **G-DEV-08** — This document is a living artefact. When a rule becomes enforced, its status
  moves from `declared` to `enforced` with the test ID. When a new subsystem lands, its
  rules are added here **before** the code, not after.

---

## 15. Rule index

| ID | Rule | Layer | Enforcement | Test | Status |
|---|---|---|---|---|---|
| G-LAW | Fail-closed on any uncertainty | all | `validator`, `runner` | TEST-LAW-01 | declared |
| G-ORDER | Validate-before-execute | L2 | compiler/executor boundary | TEST-ORDER-01 | declared |
| G-ROLE-01 | No access to game project files | L0 | path sandbox | TEST-ROLE-01 | declared |
| G-ROLE-02 | `subprocess` import boundary | L0 | static import test | TEST-ROLE-02 | declared |
| G-ROLE-03 | Write-path allowlist | L0 | `store`, adapters | TEST-ROLE-03 | declared |
| G-ROLE-04 | No fix recommendations in report | L0 | report schema | TEST-ROLE-04 | declared |
| G-ABS-01..08 | Absolute prohibitions | L0 | denylist + design | TEST-ABS-* | declared |
| G-AUTH-01..06 | Authorisation model | L0 | profile loader, epoch check | TEST-AUTH-04 | declared |
| G-PER-01..06 | Perception boundary & redaction | L1 | packet builder, sink | TEST-PER-02/03/05 | declared |
| G-INJ-01..05 | Screen text is data, never instruction | L1/L2 | prompt builder, validator | TEST-INJ-04 | **release gate** |
| G-VAL-01..09 | Proposal validation | L2 | `PlanValidator` | TEST-VAL-* | declared |
| G-BLAST-01..04 | Blast-radius classes | L2 | validator + ledger | TEST-BLAST-01 | declared |
| G-DEC-01..05 | Confidence & degradation | L3 | scorer, fallback | TEST-DEC-* | declared |
| G-SES-01..07 | Session, focus, attention, estop | L4 | guards, supervisor | TEST-SES-* | declared |
| G-VERD-01..06 | Verdict integrity | L5 | verifier import graph | TEST-VERD-01 | **release gate** |
| G-AUD-01..06 | Auditability | L6 | sink, manifest | TEST-AUD-02 | declared |
| G-DEG-01..05 | Degradation behaviour | L2/L3 | registry, ledger | TEST-DEG-01 | declared |
| G-DEV-01..08 | Dev-time rules for coding agents | process | CI + review | TEST-DEV-01 | declared |

**Status vocabulary:** `declared` = committed to and must be implemented by the phase
shown; `enforced` = implemented and covered by the named passing test. No rule may be
marked `enforced` without a green test.

---

## 16. Phase obligations

| Phase | Guardrail work that is **due**, not optional |
|---|---|
| **P0** | This document committed. Path-sandbox + import-boundary tests scaffolding. TEST-DEV-01 grep test. `authorized_use` schema stubbed and rejecting. |
| **P1** | L6 audit spine: append-only sink, manifest, redaction filter, TEST-AUD-02, TEST-PER-03. L0 authorisation validation enforced. |
| **P2** | L1 packet boundary exists (even if Tier1 unused): minimisation + redaction + TEST-PER-02/05. |
| **P3** | L4 complete: G-SES-01..07, TEST-SES-01..04, estop < 100 ms, no-input-while-unarmed. G-ABS-04 input classifier. |
| **P4** | L5 verdict integrity: G-VERD-01..06 + TEST-VERD-01. UNKNOWN path proven. L3 confidence bands. |
| **P5** | G-AUTH-01..06 enforced with TEST-AUTH-04. G-BLAST-01/02 dry-run listing. |
| **P6** | L2 validator: G-VAL-01..09, G-INJ-01..05 + **TEST-INJ-04 fuzz ≥ 50 cases**, G-DEG-01..05. **P6 cannot be declared complete without the injection fuzz gate green.** |
| **P7** | G-AUD-03..06, G-VERD-03, report separation. |
| **P8 (UI)** | UI may not add a capability, relax a guard, or expose a control that bypasses L2/L4. UI is a client of the ControlApi only. |

---

## 17. Open questions for the owner

1. **Offline enforcement (G-AUTH-03).** Ship with a mandatory `WARN` in the report, or
   attempt real enforcement with a false-positive risk? *My recommendation: warn-only,
   documented as a limitation. Enforcement that guesses wrong destroys trust in the tool.*
2. **Operator-absence detection (G-SES-06).** True absence detection needs a separate
   agent/session. Accept the v1 limitation (human-input heuristic) or defer the requirement?
   *Recommendation: accept and document.*
3. **C3 consent granularity (G-BLAST-02).** Per-scenario allowlist, or per-action-prompt?
   *Recommendation: per-scenario allowlist, with the dry-run listing printed each run.*
4. **Deviation authority (G-DEV-07).** Confirm the owner is the only party who may approve
   a guardrail relaxation ADR.

---

## 18. Non-goals of this document

It does not specify prompt wording, model choice, or planner internals. Those are
implementation details that may change freely **provided** L0–L6 hold. A prompt change
may never be the reason a rule becomes unenforced — if a guard depends on the model
cooperating, it is not a guard.
