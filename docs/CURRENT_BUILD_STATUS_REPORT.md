# Frame Forge — Current Build Status Report

**Audit date:** 2026-10-04
**Audit mode:** read-only. **No input was injected. No game was launched. No configuration was changed.**
**Repository root:** `D:\FrameForge\FrameForgeAgent`

## Build identity

| Field | Value |
|---|---|
| Branch | **none — not a git repository** (`git rev-parse` → rc=128) |
| Commit | **none** |
| Uncommitted changes | **not applicable — no VCS** |
| Python | 3.14.7 (Windows-11-10.0.26300-SP0) |
| pip | 26.2.1 |
| ffmpeg | 9.0.1 (on PATH) |
| Platform | Windows 11 build 26300, i5-8250U / 12 GB |
| Displays | **1 monitor at audit time** (1920x1080 primary). A second display was present earlier in the session and is now disconnected. |
| Test result | **511 headless pass** (28.8 s); **33 hardware pass + 1 guarded skip** |
| Lockfile | `requirements.lock` — 20 pins. Installed: 128 packages (includes transitive dev deps). |

**Reproducibility: PARTIAL.** A new developer can `pip install -r requirements.lock` and
run the suite, but there is no version control, so there is no history, no diff baseline,
and no way to attribute a change to a commit.

Commands used (all read-only or test-only):

```
python -m pytest tests/unit tests/integration -q      -> 511 passed in 28.76s
python -m pytest tests/hardware -q --run-hardware     -> 33 passed, 1 skipped
python -m frameforge.cli.main validate                -> 2 profiles, 3 scenarios valid
python -m compileall src/frameforge                    -> clean
```

---

# Part 1 — Executive summary

## UPDATE — critical safety wiring completed

Six blockers addressed since this report was first issued. Verified below.

### Deleted bypass scripts

| Script | Reason |
|---|---|
| `scripts/probe_estop_hook.py` | Called `SendInput` directly at lines 101-105 with no target session, guard, or protected-window check |
| `scripts/probe_input_timing.py` | Called `SendInput` directly at lines 37, 57, with a hard-coded `(300, 300)` |

Both are deleted. Their diagnostic value is preserved by the tests that replaced them.
All eight remaining scripts are read-only (verified by AST scan).

### Exclusive input authority — now enforced by CI

`tests/unit/test_input_authority_static.py` (6 tests) asserts, by AST walk over
`src/`, `tests/`, `scripts/`, `fixtures/`:

* only `src/frameforge/actions/safety.py` may call `SendInput` / `keybd_event` /
  `mouse_event` / `SetCursorPos`;
* that module **is** still calling `SendInput` (a vacuous allowlist would be worse than
  none);
* no injection library is imported anywhere;
* no hard-coded absolute screen point exists in `tests/`, `scripts/` or `fixtures/`;
* the two bypass scripts do not exist;
* no script constructs a live (non-dry-run) input port.

### Single enforced runtime path

```
CLI / runner / scenario / (future GUI)
        ↓
   LiveGate                 actions/runtime.py  — the only authoriser
        ↓  authorize():  ACTIVE? token? identity? capture? foreground?
        ↓                gameplay? deadline? point in region? WindowFromPoint?
        ↓                protected window? profile permits?
   ExecutionStateMachine → TargetSession → arm token → TargetGuard
        ↓
   InputSafetyController → approved backend → Windows
```

Wiring evidence (`grep` of `src/frameforge/tasks/runner.py` and `cli/main.py`):

| Symbol | Present |
|---|---|
| `ExecutionStateMachine` | yes |
| `TargetSession` | yes (`target_session`) |
| `begin_countdown` / `confirm_active` | yes (`arm_for_live_input` / `confirm_live_input`) |
| `ProtectedRegistry` | yes (`protect_current_process` + 7 window classes) |
| `_build_live_gate` | yes, called when a target registers |

Three real defects were found and fixed while wiring:

1. **The gate was built only at arm time**, so the entire run before arming was unguarded.
   It is now built the moment a target registers.
2. **The executor's guard was never attached**, because the gate is built before the
   executor exists. Now attached at executor construction.
3. **The executor could be constructed with no controller or policy at all.** It now raises
   at construction — fail-closed one layer earlier than before.

### CLI

`frameforge status` — read-only; prints state, input lock, target identity (hwnd, pid,
executable path, class, title, client rect, monitor, dpi) and the protected set. Never
injects, never focuses.

`frameforge arm --task ... --profile ... --attest "..."` — the only route to live input.
`--attest` is **required with no default**; the CLI exits 2 without it. Verified by running
it.

Both use a `_DryRunPort` that cannot inject, so neither command can shoot.

### Tests

| Layer | Count | Result |
|---|---|---|
| Component (unit) | 511 | pass |
| **Runner→backend integration (new)** | **19** | **pass** — `tests/integration/test_live_gate_integration.py` |
| Static input-authority guard (new) | 6 | pass |
| **Total headless** | **537** | pass in ~42 s |

The new integration tests start from `ScenarioRunner.prepare()` — the real composition
root — not from the state machine directly. They prove: runner starts input-locked; target
registration alone does not unlock; arm without attestation is refused; countdown never
dispatches; unknown gameplay blocks; forbidden intents are refused even when ACTIVE is
forced; the cursor position cannot influence the outcome; dispatch refuses and emits
nothing; emergency stop releases and blocks; a run with no profile has no session at all;
and the executor cannot be constructed without a controller.

### Gate status after this work

| Item | Scope now |
|---|---|
| P0 no locationless click | `COMPLETE_AND_VERIFIED_MOCK` |
| P0.5 no fixed coordinates | `COMPLETE_AND_VERIFIED_MOCK` |
| P1 target session mandatory | `COMPLETE_AND_VERIFIED_MOCK` |
| P2 WindowFromPoint x2 | `COMPLETE_AND_VERIFIED_MOCK` |
| P3 protected registry | `COMPLETE_AND_VERIFIED_MOCK` |
| P4 DPI awareness | `COMPLETE_AND_VERIFIED_MOCK` |
| P5 topology on dispatch | `COMPLETE_AND_VERIFIED_MOCK` |
| P6 target-scoped capture | `COMPLETE_AND_VERIFIED_MOCK` |
| P7 UI Automation | `NOT_IMPLEMENTED` |
| P8 user-armed state machine | `COMPLETE_AND_VERIFIED_MOCK` |
| P9 emergency stop | `COMPLETE_AND_VERIFIED_MOCK` |
| Notepad dual-monitor POC | `NOT_IMPLEMENTED` |
| Read-only diagnostic panel | `COMPLETE_AND_VERIFIED_MOCK` |

P4 was verified by reading `dpi_awareness()` on this machine: `per_monitor_v2` is in force,
both monitors report dpi/scale/work-area, and dpi+scale are part of the topology fingerprint.
P5 and P6 were verified with tests that force a disconnect, a reorder and a 96->144 scaling
change, and that present a stale, black and hwnd-mismatched capture.

"(mock path)" means verified through the real runner with a recording input backend. **None
has been verified on a live desktop**, because live desktop testing remains prohibited.

### Live desktop input status

**DISABLED.** No live input was performed during this work. The Notepad POC has not been
run. It requires a second monitor (currently disconnected) and your explicit approval.

---

### Verification scope labels

Every gate carries one of these. A bare "verified" is not a permitted label, because the
dangerous reading is "verified against a real desktop" when the evidence was a fake backend.

| Label | Meaning |
|---|---|
| `COMPLETE_AND_VERIFIED_MOCK` | Verified through the real runner and the real state machine against a recording input backend. No Windows input was sent. |
| `COMPLETE_AND_VERIFIED_LIVE_ISOLATED` | Verified on a real desktop inside an isolated test session or VM. |
| `COMPLETE_AND_VERIFIED_LIVE_USER_DESKTOP` | Verified on the operator's own desktop with live input. |
| `IMPLEMENTED_NOT_VERIFIED` | Code exists; no test exercises it end to end. |
| `PARTIAL` | Some of the gate is implemented; the remainder is absent. |
| `NOT_IMPLEMENTED` | Absent. |

**Nothing in this build is `COMPLETE_AND_VERIFIED_LIVE_USER_DESKTOP` or
`COMPLETE_AND_VERIFIED_LIVE_ISOLATED`.** Every verified gate below is mock-path only.

## Notepad POC failure triage

The "8/12" figure in earlier reports was stale. The most recent run
(`ff-20261004-072409-368f5e`) recorded **7 pass / 2 fail of 9 steps**, and both failures
had a single shared cause.

### The failures were not OCR failures

The report's `actual` field read `File\nEdit\nView` for both failing steps, which looked
like OCR failing to find the typed marker. It was not. The saved evidence frame
(`frames/43371820_state_menu_file.png`) was inspected directly: the document body contains
only a single `w` (restored Notepad session state) and **no trace of the typed text**. The
text was never on screen. OCR was reporting the truth.

Classifying the report was therefore misleading in both directions — it recorded a
perception failure where the actual defect was in input delivery.

### Step classification

| Step | Verify | Class |
|---|---|---|
| `notepad_ready` | `anchor_visible` | target safety |
| `no_save_prompt_is_showing` | `anchor_absent` | target safety |
| `type_probe_text` | `text_matches` | visual/OCR |
| `probe_text_visible` | `text_matches` | visual/OCR |
| `clear_the_document` | `always` | neutral |
| `delete_selection` | `text_absent` | visual/OCR |
| `probe_text_cleared` | `text_absent` | visual/OCR |

The safety gate does **not** depend on OCR. Both safety steps are landmark assertions on
the window itself, and the hwnd/pid/session/foreground/protected-window/WindowFromPoint
checks run inside the guard independent of any verifier. OCR is used only for outcome
verification, which is the intended division.

### Two defects found and fixed

1. **Keyboard input bypassed `TargetGuard` entirely.** The guard was consulted only for
   mouse primitives. A keystroke goes to whatever holds foreground focus, so an unguarded
   keypress while Hermes or a browser was in front typed into *that* — the same failure an
   unguarded click causes, reached by a different route. `TargetGuard.validate_keyboard()`
   now requires the registered target to hold focus, rejects a protected foreground window,
   and re-checks the window under the cursor. Keyboard primitives also now undergo the
   topology check. An unguarded keystroke is refused with zero input emitted.

2. **`scan` text mode was a silent no-op.** It expanded text to `Key` values and emitted
   ordinary virtual-key events with `wScan=0` — byte-for-byte identical to `unicode` mode,
   while the profile and docs described it as emitting hardware scancodes. Any target
   reading raw scancodes got nothing, with no error. `PrimitiveType.SCANCODE` now emits
   `wVk=0` + `KEYEVENTF_SCANCODE` with the extended-key flag where required, backed by a
   real set-1 scancode table distinct from the virtual-key table.

Both were silent. Neither raised, and the existing suite passed throughout — the evidence
frame was the only thing that distinguished "OCR could not see it" from "it was never
there".

The Notepad profile's `text_method` was `scan`, a workaround for defect 2. Notepad's
editor is a standard Win32 EDIT control that consumes `WM_CHAR`, so it is now `unicode`,
which is both correct and layout-independent.

### Status

The Notepad POC remains `NOT_IMPLEMENTED` for live validation: the defects are fixed and
unit-tested, but no live run has been performed, so no claim is made that the round trip
now passes.

## Failure classification requires evidence inspection

**The rule: a failed observation does not, by itself, establish a perception defect.**
Classify a verification failure only after inspecting the evidence artifact to determine
whether the asserted UI state actually occurred.

| Apparent result | Evidence frame shows | Domain | Who fixes it |
|---|---|---|---|
| OCR cannot find expected text | the text **is** visible | `perception` | OCR, UIA extraction, region selection, thresholds |
| OCR cannot find expected text | the text is **absent** | `delivery` | input routing, focus, target authority, event semantics |

The same apparent result has two opposite remediations. Defaulting to either one sends a
reader to the wrong component — which is how the Notepad round-trip was misdiagnosed as a
perception fault when the text had in fact never reached the screen.

### This is enforced in code, not just documented

`frameforge/qa/faultdomain.py` provides `classify_failure()`, which **refuses** to name a
domain unless given an inspected observation and the artifact it came from:

* no inspection supplied → `UNDETERMINED`, plus what artifact would settle it;
* an inspection claimed with no artifact named → refused, back to `UNDETERMINED`;
* guarded stop → `GUARDED`, described as correct behaviour rather than a defect.

`Report.first_divergence` carries `fault_domain`, `fault_basis`, `evidence_backed` and
`evidence_required`, and the rendered markdown states the evidentiary limit inline so a
reader who skims cannot mistake a default for a finding.

**No `remediation` field exists in the report.** Guardrail G-ROLE-04 forbids the report from
suggesting fixes; naming a fault domain is diagnosis of a running target, which is Frame
Forge's job, but a fix is a code change, which is Herman/Cline's. The guidance text lives in
the module and in the basis line a human reads.

Applies from P7 onward: any new verifier inherits the rule by calling `classify_failure`
rather than inventing its own failure taxonomy.

## Live POC preflight (dry-run) — the runnable claim, verified

The live POC still requires explicit approval. Everything short of the injection boundary
was exercised for real against the actual Notepad window, using `run --dry-run`, which wires
the entire pipeline and injects nothing.

**Run `ff-20261004-145327-141e10`** — real `notepad.exe`, real launch, real acquisition:

| Check | Result |
|---|---|
| `doctor` | 0 BAD; uniform 96 dpi across 2 monitors; active session; 3840x1080 virtual desktop |
| target acquired | `Notepad.exe[21728] 'Notepad' '*ww - Notepad' hwnd=3017096` |
| readiness landmark | `menu_file` matched, score 1.000 |
| save-prompt gate | asserted absent |
| all 5 interactive actions reached the guard | KeyPress, Click, type_text, Hotkey, KeyPress |
| primitives dispatched | 12 |
| **primitives delivered to OS** | **0** |
| verdict | 7 pass / 2 fail (expected: no input, so the text is correctly absent) |
| `first_divergence.fault_domain` | `undetermined`, `evidence_backed: false` |

The run reaching completion with 12 dispatched and 0 delivered is the proof that the POC is
runnable end to end, and that the audit trail now says truthfully what happened.

### Three silent gaps the preflight exposed

1. **`PrimitiveType.UNICODE` had no declarative action.** The executor refuses any primitive
   it cannot describe, so *all* unicode-mode typing was refused as `undeclarable_primitive`.
   No text could reach any target. The Notepad profile had just been moved to `unicode`, so
   this would have failed again on the next run — for a different reason, still silently.
2. **`PrimitiveType.SCANCODE` had no declarative action either** — the primitive added for
   the scancode fix was permanently unauthorisable, which would have made scan mode useless
   for exactly the game surfaces it exists to serve.
3. **The audit trail's `sent` field meant "dispatched", not "delivered."** A `--dry-run`
   reported `"sent": 12` while guaranteeing nothing reached Windows. Anyone auditing
   "did input reach the operator's machine" would have read the opposite of the truth.

All three are fixed. `ToolResult.delivered` carries the true count from `send_batch`, and
the audit records `dispatched` and `delivered_to_os` as separate facts. A test walks every
`PrimitiveType` and fails if one lacks a declarative action, so the next kind cannot be added
without one.

**Scope of this preflight: `COMPLETE_AND_VERIFIED_MOCK` for the guard path, and a real
acquisition of the real Notepad window. It is NOT live input, and it does not verify the
typed-text round trip.**

## Live POC result — run `ff-20261004-153124-4cba51`

**Verdict FAIL, 7 pass / 2 fail.** This is the first live input ever emitted by this system.
19 input events, 7 delivered to Windows, target `Notepad.exe[9896]` hwnd 46533056.

### Post-run classification (evidence-backed)

`first_divergence.fault_domain` was correctly **`undetermined` / `evidence_backed: false`**.
The evidence frame was then inspected, which is what permits a domain to be named:

* Artifact: `frames/72606169_state_menu_file.png`
* Inspected: the document body is **empty** — only a caret. `FFPROBE7421` is **absent**.
* Absent ⇒ the action never took effect ⇒ **`DELIVERY`**, evidence-backed.

It is not a perception failure. OCR reported the truth, again.

### What actually happened

`input_audit.json` gives the exact mechanism. Event 8:

```
{"event_id": 8, "event": "unicode", "count": 0, "detail": "input disarmed"}
```

The typed text was **refused by the arm gate** — never dispatched to Windows at all. The
event log shows the repeating signature across the whole run:

| id | event | count | detail |
|---|---|---|---|
| 3 | cleanup | 33 | — |
| 4 | mouse_move_abs | 0 | **input disarmed** |
| 5 | mouse_button | 0 | **input disarmed** |
| 6 | mouse_button | 1 | delivered |
| 7 | cleanup | 33 | — |
| 8 | unicode | 0 | **input disarmed** |

Every `cleanup` is followed by presses refused as *"input disarmed"*.

**Root cause:** `InputSafetyManager.release_all()` ends with `self.enabled = False`.
`ActionExecutor` calls `_release_held()` on **every** batch exit, and the controller's
`execute()` calls `release_all()` after each dispatched primitive. So the very first action
disarmed the manager, and every subsequent press was refused. Only the initial Escape
(events 1–2) and a few releases got through, because releases bypass the arm gate by
design.

This is a single defect with three visible symptoms: the typed text never arrived, the
click's move was refused, and roughly half the primitives were silently dropped. It was
invisible to the entire mock test suite because every mock port accepts primitives
regardless of its armed state.

### Classification is now evidence-backed

The classifier refused to name a domain until the artifact was inspected, and the artifact
settled it. That is the rule working as intended, on a real run, for the first time.

**This run does NOT verify the typed-text round trip.** It verifies that the guard path
holds on a live desktop: no protected-window input, no wrong-window input, target identity
verified 14 times with 0 violations, no key or button left down, input layout unchanged.

## Live POC: four defects found and fixed on the user's desktop

Six live runs, all recorded. Four defects, each silent, none visible to the mock suite.

### 1. `release_all()` disarmed the manager (run `ff-20261004-153124-4cba51`)

`InputSafetyManager.release_all()` ended with `self.enabled = False`. The executor releases
on every batch exit and the controller releases after every dispatched primitive, so the
**first action of the run disarmed input** and every later press was refused with
*"input disarmed"*. The typed text never left the machine. Evidence:

```
id=8  unicode  count=0  detail="input disarmed"
```

Every `cleanup` in the log is followed by presses refused the same way. Releases bypass the
arm gate by design, so the sweep *looked* healthy.

**Fixed:** a release sweep is a reconciliation of what is physically held, not a revocation
of press authority. Stopping input is a separate explicit act (`disarm()`), which emergency
stop still calls. Refused presses are now counted (`blocked_presses`) and surfaced in the
audit, because a dropped action that is invisible in the summary is how this hid behind a
plausible "7 pass / 2 fail".

**Why 600+ tests missed it:** `FakeInput.send()` consulted only its own `_enabled` flag and
never the safety manager's, so the fake was *more permissive* than the real adapter. It now
consults both and arm/disarms them in lockstep — a mock more permissive than the thing it
mocks cannot fail when the real thing does.

### 2. Unicode typing was a single unspaced primitive

`_compile_type_text` emitted the whole string as one `UNICODE` primitive, so the scenario's
declared `interval_ms` was accepted by the schema and silently discarded. Now one primitive
per character, so the gap is real. Default interval raised 12 ms → 35 ms.

### 3. Unicode injection sent no key-up (run `ff-20261004-155901-b805a1`)

`_unicode_char` sent only `KEYEVENTF_UNICODE` **down**, never the paired key-up. Twelve
characters were delivered to a correctly identified, correctly focused Notepad with 0
violations, and the document did not change. Now submitted as a down/up pair in one atomic
`SendInput`.

### 4. An aborted run reported PASS (runs `…7c6127`, `…015463`)

`overall_from()` only sees verdicts from steps that executed, so a run that died during the
typing step reported **`PASS` with four passing setup assertions** while `state` was
`failed`. Two of those runs were green for reasons that had nothing to do with success. A
green verdict now requires `state` to be a completed run.

### Current live finding: Notepad ignores VK_PACKET

`WM_GETTEXT` on Notepad's editor (class `RichEditD2DPT`, hwnd 592230) returns the ground
truth directly, with no OCR involved:

```
text='FPROBE74'   length=8
```

That is residue from an earlier run. The current run's characters are not landing at all.
The control is `RichEditD2DPT`, which ignores the `VK_PACKET` events `KEYEVENTF_UNICODE`
produces. The profile has been set to `scan`, which now emits genuine scancodes.

Note the history: an earlier revision also said `scan`, but as a *workaround* for the
silent no-op fixed in `fd3fe92`. That defect is fixed, so this is now a measured choice.

### The human-input guard fired, correctly

The first scan run (`ff-20261004-160108-db7c86`) aborted with `HumanInputDetected` after
**exactly one** scancode: the operator moved the mouse. The guard halted mid-sequence rather
than firing the remaining eleven characters at a moving cursor. This is the behaviour the
guard exists for, observed live for the first time.

### What is now verified on the live desktop

| Claim | Status |
|---|---|
| No input to a protected window | verified, 6 runs |
| No input to a wrong window | verified, 6 runs |
| Target identity (hwnd/pid/pid-image) | verified, 14 checks/run, 0 violations |
| No key or button left down | verified every run |
| Input layout unchanged | verified (`HKL=0x40090409` before and after) |
| Human-input detection halts a live sequence | **verified live** |
| Typed-text round trip | **NOT verified** — blocked on the VK_PACKET finding above |

**Scope: `COMPLETE_AND_VERIFIED_LIVE_USER_DESKTOP` for the guard path.
The typed-text round trip remains `IMPLEMENTED_NOT_VERIFIED`.**

## Seventh live run: the click was landing on an open menu popup

Run `ff-20261004-160852-cc2611`. Two defects found; one is a **safety** defect.

### 1. Frame Forge was detecting itself as a human (my bug, from the previous run)

`HumanInputDetected` fired after exactly one scancode, twice in a row. Cause: the executor's
hand-written list of "kinds that count as agent input" omitted `SCANCODE`, which was added
in the same session. Without it the idle-time baseline was never rebased after Frame Forge's
own keystroke, so the next sample saw idle time collapse and concluded a human had acted.

**Fixed** by deriving the set from `PrimitiveType` itself rather than listing it:

```python
_AGENT_INPUT_KINDS = frozenset(PrimitiveType) - {PrimitiveType.GAMEPAD_STATE}
```

A list that must be updated when a kind is added is a list that will be forgotten again —
and forgetting it makes the guard *fail closed on the system's own input*, which reads as
"the operator interfered" and sends the reader hunting for the wrong cause.

### 2. SAFETY: an open menu popup was an approved click target

With that fixed the run completed: **56 scancodes delivered, 0 violations**, and the document
buffer was still unchanged (`WM_GETTEXT` → `'FPROBE74'`, length 8).

`GetGUIThreadInfo` showed why:

```
hwndActive : 4262424 'Notepad'
hwndFocus  : 723232  'InputSiteWindowClass'      <- NOT the editor
```

and the click point resolved to:

```
client(400,450) -> screen(536, 580)
WindowFromPoint -> 3868326 'Microsoft.UI.Content.PopupWindowSiteBridge'
root            -> 'PopupHost'  rect=(207,237,569,694)  visible=1
```

**A `PopupHost` menu was open over the editor for the entire run.** Every point inside the
editor region — (400,400), (400,300), (400,450) — resolved to that popup, which spans
(207,237)-(569,694). The scenario's `focus_the_editor` click therefore landed on a menu, not
on the text control, and the 56 scancodes went to a window with keyboard focus on
`InputSiteWindowClass`.

This also explains the OCR output seen in every earlier run: `Edit View`, `Select all`,
`Delete`, `Undo`, `Insert table` are **menu items**, not document text. The verifier was
reading a menu and reporting it as the document body.

**Why the guard permitted it.** `TargetGuard.validate_point` accepts a window whose root
hwnd or pid matches the registered target. `PopupHost` is a Notepad-owned window, so it
passed every check: correct pid, correct root owner, inside the client rect, foreground
correct. The guard authorised a click on a transient menu overlay because *ownership* was the
only test, and ownership says nothing about whether the surface is the one the scenario meant.

That is a real hole and it is not specific to Notepad. Any application that opens a popup
over its own client area produces the same shape: a click the scenario believes focuses an
editor instead activating a menu item.

### Fixed and verified live

`Verdict.POPUP_OVERLAY` is now a denial on both paths. `_is_transient_overlay()` matches
framework popup class names and overlay titles as lowercase substrings — WinUI, WPF, classic
Win32 menus, combo lists, tooltips, IME — so it stays engine-agnostic instead of becoming a
per-application list.

The point path checks it **before** the ownership test, because an owned popup passes
ownership; that ordering is pinned by a test. The keyboard path resolves the focused window
(`GetFocus` as fallback) and refuses a keystroke an overlay would consume.

Verified live on run `ff-20261004-161709-031003`. A freshly launched Notepad covers its
editor with a `PopupHost`, and the guard refused with zero input dispatched:

```
target_validation_failed: the window under the point is a popup/menu overlay
('Microsoft.UI.Content.PopupWindowSiteBridge', title 'PopupHost')
dispatched=0  delivered_to_os=0
```

19 new tests in `tests/unit/test_popup_overlay_guard.py`.

## INVESTIGATION — reported right-click context menu inside Hermes

**No live input was emitted during or after this investigation.** No POC was run.

### Observed live evidence

A right-click context menu reportedly opened inside the Hermes window on the laptop monitor
during a dual-monitor Notepad test. What the recorded evidence actually contains, across all
36 run directories:

| Query | Result |
|---|---|
| `mouse_button` events delivered (`count=1`) | 32, **all left button** |
| Right-click events, any run | **0** |
| `allow_right_click = True` anywhere in `src/` | **none** |
| Runs with any right-click in `events.jsonl` | **0** |

The audit schema does not record which button a `mouse_button` event carried, so button
identity had to be established from the guard path instead of from the log. Doing so:

* `InputSafetyManager.__init__(allow_right_click: bool = False)` — default off.
* `_screen()` returns `Violation.RIGHT_CLICK_NOT_ALLOWED` for any `button == "right"` and
  `down` while the flag is false, **before** `_inject` is reached.
* No constructor in `src/` passes `allow_right_click=True`.

**Conclusion: the current code cannot emit a right-click.** Guardrail G-ABS-11 blocks it
before injection. Therefore the reported context menu was **not** produced by a right-click
from this code path as it stands today.

That leaves two possibilities, and the evidence does not distinguish them:

1. The context menu came from a path not represented in `runs/` — for example one of the
   two diagnostic probe scripts deleted in `5c680a1` (`scripts/probe_estop_hook.py`,
   `scripts/probe_input_timing.py`), which called `SendInput` directly with no guard at all.
   Those scripts predate every run directory recorded here.
2. The context menu was produced by a keypress, not a mouse button — `VK_APPS` / `Menu`
   / `Shift+F10`. All three are refused by `_screen()` as `Violation.MENU_KEY`, so this
   also requires a non-current path or an older build.

**Artifact needed to decide:** the run ID or timestamp of the reported incident, so the
matching `input_audit.json` can be read. Without it the specific event cannot be attributed.
Classification: **UNDETERMINED** as to the emitting path; **NOT a perception or verifier
issue** in either case.

### Exact implementation defect (confirmed by reading the code)

`InputSafetyManager._abs_norm` — `src/frameforge/actions/safety.py`:

```python
def _abs_norm(self, x: int, y: int) -> tuple[int, int]:
    vw, vh = virtual_size()                       # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
    nx = int(x * 65535 / max(1, vw - 1))          # <-- no origin term
    ny = int(y * 65535 / max(1, vh - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))
```

Measured geometry on this machine:

```
SM_XVIRTUALSCREEN  (76) = 0        SM_CXVIRTUALSCREEN (78) = 3840
SM_YVIRTUALSCREEN  (77) = 0        SM_CYVIRTUALSCREEN (79) = 1080
primary = 0,0,1920,1080            negative origin: NO
```

The formula divides by virtual **size** but ignores virtual **origin**. On *this* layout the
origin is (0,0), so the term is zero and the two forms are numerically identical — measured
error 0 px at every test point. The defect is therefore **latent here and not the cause of
the reported incident**, but it is real: with the external monitor placed to the *left*
(origin −1920) a planned point of (1919, 500) lands at (−2, 499) instead of (1919, 500) — a
**1921 px** error that puts the cursor on the wrong display entirely.

Correct form: `nx = (x - vx) * 65535 / (vw - 1)`.

### SendInput payload (as emitted)

* `MOUSEEVENTF_ABSOLUTE` — **set**
* `MOUSEEVENTF_VIRTUALDESK` — **set**
* `MOUSEEVENTF_MOVE` — **set**
* flags: `MOVE | ABSOLUTE | VIRTUALDESK` = `0x0001 | 0x8000 | 0x0001` as OR'd in `_inject`
* button down and button up are separate `_MOUSEINPUT` submissions via `_button()`, whose
  down/up flag pairs come from `_MOUSE_BUTTON_FLAGS`.

So both flags that matter are present, and normalisation spans the whole virtual desktop
when the origin is zero. **Primary-monitor normalisation and a missing VIRTUALDESK flag are
both ruled out** for this geometry.

### Guard gap (confirmed, and the real finding)

The executor's pre-button-down check validates the **planned** point, never the **actual**
cursor position.

```python
if (primitive.kind is PrimitiveType.MOUSE_BUTTON and primitive.down
        and self.target_guard is not None
        and self._verified_move is not None):
    result = self.target_guard.validate_point(self._verified_move)   # planned, not actual
```

`_verified_move` is assigned from the plan at the `MOUSE_MOVE_ABS` primitive and is never
refreshed from the OS. `GetCursorPos` appears **nowhere** in `executor.py` or `target.py`;
`cursor_position()` exists in `safety.py` but is not called from either. There is no defined
tolerance for planned-vs-actual drift.

Consequence: if the cursor does not arrive where the plan said — mis-normalisation, a
changed display layout, a concurrent input source, a snapped or dragged window — the guard
validates a point that is not where the click lands, and the button goes down wherever the
cursor actually is. `WindowFromPoint` is evaluated at the *planned* coordinate, so it reports
the target even when the cursor is elsewhere.

This is the mechanism by which input could reach Hermes even with every other check passing.
It is a **design gap, not an observed event** in the recorded runs.

### Answer to "how could a target on the external display produce a click inside Hermes"

Not reproducible from the current code. The path would require the cursor to be somewhere
other than the planned point at button-down, and the guard to validate the planned point
rather than the actual one — which is exactly the gap above. With
`MOUSEEVENTF_VIRTUALDESK` set and a zero origin, the normalisation itself is correct on this
layout, so the cursor does arrive where planned unless something external moved it.

### Regression tests added

`tests/unit/test_multimonitor_target_routing.py` — 13 passed, 2 xfailed, representing the
exact topology (Hermes on primary, Notepad on external, 3840×1080 virtual desktop):

* external-monitor points normalise mid-range rather than saturating;
* the 1919→1920 seam is monotonic (a fold means primary-only clamping);
* a negative origin maps the virtual origin to 0;
* `ABSOLUTE | VIRTUALDESK` are both present on the move;
* button down and up are distinct payloads;
* right-click is denied by default and nothing in `src/` enables it;
* the executor and guard never read the cursor position (**open defect**, asserted so the
  test fails when fixed);
* no tolerance comparison exists (**open defect**);
* `_abs_norm` uses the origin (**xfail**, open defect);
* the normalised pair is recorded in evidence (**xfail**, open defect).

### Status

* **Live input emitted after the stop instruction: NO.** No POC was run; only read-only
  queries (`GetSystemMetrics`, `EnumWindows`, `WindowFromPoint`, `GetCursorPos`,
  `GetGUIThreadInfo`, `WM_GETTEXT`) were issued.
* **Dual-monitor Notepad POC: NOT VERIFIED.**
* Incident attribution: **UNDETERMINED** — needs the run ID or timestamp.

## Overall readiness rating

**INTERNAL ALPHA — SAFE FOR MOCK TESTING. NOT SAFE FOR ANY LIVE DESKTOP INPUT.**

Not "prototype": the safety-critical components are implemented and unit-tested. Not
"safe for limited isolated testing" either, for one reason given in full below.

## The single most important finding

**The user-armed state machine and `TargetSession` are not wired into the run path.**
Evidence: `grep` of `src/frameforge/tasks/runner.py` and `cli/main.py` for
`ExecutionStateMachine`, `TargetSession`, `begin_countdown`, `confirm_active`,
`target_guard`, `arm_token` returns **false for every one**. They are implemented
(`actions/arm.py`, `actions/target.py`) and tested (28 tests), but **a live run does not
construct either**.

Consequently: the tests prove the *components* work in isolation. They do **not** prove
that a real run passes through them. Every safety gate below is therefore marked
`IMPLEMENTED_NOT_VERIFIED` or `PARTIAL` — never `COMPLETE_AND_VERIFIED` — because the
end-to-end path has not been exercised.

## Recommended activity today

- Mock-backend and headless test development.
- Wiring the arm machine into the runner (Phase A).
- Removing the two diagnostic scripts that bypass the safety layer.

## Explicitly prohibited today

- Any live mouse or keyboard injection.
- Any game testing, online or offline.
- The Notepad POC.
- Relying on `scripts/probe_estop_hook.py` or `scripts/probe_input_timing.py`.

## Top 10 blockers (by severity)

1. **CRITICAL** — Arm/TargetSession not wired into the run path (`runner.py`, `cli/main.py`).
2. **CRITICAL** — Two diagnostic scripts call `SendInput` directly, bypassing every guard (`scripts/probe_estop_hook.py:101-105`, `scripts/probe_input_timing.py:37,57`).
3. **HIGH** — No git repository; no build identity, no history, no rollback.
4. **HIGH** — No UI Automation; targeting is coordinate-only (P7 `NOT_IMPLEMENTED`).
5. **HIGH** — Notepad scenario failing (OCR recall); the only third-party proof is not green.
6. **MEDIUM** — `TargetGuard` is injected into the executor but no live path supplies one.
7. **MEDIUM** — Single monitor now; the dual-monitor acceptance scenario cannot be run.
8. **MEDIUM** — No GUI at all; arming is impossible without one.
9. **LOW** — `python -m compileall` clean but no linter/typechecker is configured or run.
10. **LOW** — `requirements.lock` has 20 pins but 128 packages are installed (transitive drift unpinned).

## Top 10 verified working capabilities

1. Append-only redacted audit log (`kernel/bus.py`), crash-tolerant.
2. Single-injector architecture: `SendInput` reachable only from `actions/safety.py` (AST-verified in `src/`).
3. Point-less click impossible at schema, compiler and runtime (3 layers, verified by import and by execution).
4. Defensive release of both sides of every modifier + all mouse buttons + Apps/F10.
5. Language-switch chords refused (Ctrl+Shift, Alt+Shift, Win+Space); `SYSTEM_INTENTS` unopt-outable.
6. Emergency stop, 16 ms steady-state, 3 independent paths.
7. Hardened child environment on all 8 subprocess spawn sites (CI-enforced).
8. Virtual-desktop coordinate normalisation incl. negative origins, no primary-monitor assumption.
9. Deterministic capture: canary scenario detects a planted defect, PASS when removed.
10. 511 headless tests, zero external services, ~29 s.

## Main conclusion

**Proceed with A — safety/input completion.** Not B (UI) and not C (mock scenarios).

Rationale: building a GUI on top of a run path that bypasses its own safety machine would
produce a reassuring interface over an unguarded engine. The wiring is the blocker.

---

# Part 2 — Build identity and reproducibility

Covered in the table above. Additional findings:

| Item | Status |
|---|---|
| Build command | `pip install -e ".[dev]"` — editable install; no wheel/build step defined |
| Lint / typecheck | **NOT CONFIGURED.** `pyproject.toml` declares `[tool.ruff]` but ruff is not installed and no run exists in any script |
| Static checks | Hand-rolled pytest assertions, not a separate CI |
| CI | **NONE.** No `.github/`, no pipeline |
| Secrets | `FRAMEFORGE_AI_API_KEY` from environment only; redactor covers 10 secret shapes; `Settings.redacted_dict()` excludes the env var name |
| Fresh-dev reproducibility | Partial. No VCS, no CI, transitive deps unpinned |

---

# Part 3 — Repository and architecture map

```
                    ┌──────────────────────────┐
   scenarios ──────▶│      RunDirector         │  deterministic loop, sole state owner
   (JSON/YAML)     │  kernel/director.py      │
                    └───────────┬──────────────┘
                                │
        ┌───────────────────────┼────────────────────────┐
        ▼                       ▼                        ▼
  Perception              Action layer              Report/Store
  capture + OCR          model → compiler           events.jsonl
  + anchors              → policy → controller      report.json/.md
        │                       │
        │              ┌────────▼─────────┐
        │              │ InputSafetyMgr   │  ONLY SendInput caller
        │              │ actions/safety   │  holds, watchdog, deny-list,
        │              └────────┬─────────┘  audit, layout restore
        │                       │
        │              ┌────────▼─────────┐
        │              │ Windows backend  │
        │              │ adapters/input   │
        │              └──────────────────┘
        │
   ╔════╪══════════════════════════════════════════════╗
   ║  NOT WIRED INTO THE RUN PATH (see Blockers 1 & 6) ║
   ║  actions/arm.py    ExecutionStateMachine           ║
   ║  actions/target.py TargetSession / TargetGuard     ║
   ║  perception/proposal.py  VisionProposal            ║
   ╚═══════════════════════════════════════════════════╝
```

| Path | Purpose | Lang | Status | Tests | Safety relevance |
|---|---|---|---|---|---|
| `kernel/director.py` | Loop owner, state machine, plan trace | Python | PARTIAL | integration | Orchestrates all actions |
| `kernel/bus.py` | Append-only redacted event log | Python | VERIFIED_WORKING | unit | Audit backbone |
| `kernel/states.py` | RunState + legal transitions | Python | VERIFIED_WORKING | unit | Invalid transitions raise |
| `actions/model.py` | Action schemas; `at` required | Python | VERIFIED_WORKING | unit | **P0** |
| `actions/compiler.py` | Action → primitives | Python | VERIFIED_WORKING | unit | Refuses point-less clicks |
| `actions/safety.py` | **Only** `SendInput` caller | Python | VERIFIED_WORKING (unit) | 40 unit | **Critical** |
| `actions/target.py` | TargetSession, TargetGuard, ProtectedRegistry | Python | **NOT WIRED** | 11 acceptance | **P1/P2/P3** |
| `actions/arm.py` | ExecutionStateMachine, profiles, arm tokens | Python | **NOT WIRED** | 28 unit | **P8** |
| `actions/budget.py` | Ledger, token accounting | Python | VERIFIED_WORKING | unit | Bounds work |
| `actions/estop.py` | Emergency stop, 3 paths | Python | VERIFIED_WORKING | hardware | **P9** |
| `adapters/input/sendinput.py` | Thin facade over safety mgr | Python | VERIFIED_WORKING | hardware | Gate check inside port |
| `adapters/capture/*` | mss capture, DXGI optional | Python | VERIFIED_WORKING | hardware | ~108 ms/frame |
| `adapters/ocr/*` | WinRT OCR (persistent host) | Python+PS | PARTIAL | hardware | **Known defect** |
| `adapters/window/*` | Windows identity/monitors/DPI | Python | VERIFIED_WORKING | hardware | P4/P5 |
| `adapters/vision/*` | OpenCV template match | Python | VERIFIED_WORKING | unit | Perception |
| `perception/assembler.py` | Frame→Observation | Python | VERIFIED_WORKING | unit | |
| `perception/health.py` | Capture + occlusion health | Python | VERIFIED_WORKING | unit | |
| `perception/proposal.py` | VisionProposal contract | Python | **NOT WIRED** | unit | Cannot inject by type |
| `perception/verify.py` | Postconditions | Python | VERIFIED_WORKING | unit | No AI in verdict path |
| `planning/tier0.py` | Deterministic policy | Python | VERIFIED_WORKING | unit | |
| `planning/ai.py` | Tier-1 adapter (CLI bridge) | Python | IMPLEMENTED_UNVERIFIED | unit | Model cannot widen rights |
| `planning/validator.py` | Plan validation | Python | VERIFIED_WORKING | unit | |
| `planning/budget.py` | Context-budget | Python | VERIFIED_WORKING | unit | |
| `tasks/dsl.py` | Scenario schema | Python | VERIFIED_WORKING | integration | |
| `tasks/runner.py` | Composition root | Python | VERIFIED_WORKING (no arming) | integration | **Blocker 1** |
| `tasks/launch.py` | Direct launch (opt-in) | Python | VERIFIED_WORKING | integration | G-BLAST-02 |
| `tasks/replay.py` | Recorded-plan replay | Python | VERIFIED_WORKING | unit | Determinism |
| `tasks/recovery.py` | Recovery ladder | Python | IMPLEMENTED_UNVERIFIED | — | |
| `qa/report.py` | report.json/.md | Python | VERIFIED_WORKING | integration | |
| `store/runs.py` | Evidence store, ffmpeg | Python | VERIFIED_WORKING | integration | |
| `profiles/*` | Profile schema + 7 presets | Python | VERIFIED_WORKING | integration | |
| `api/server.py` | Local control API | Python | **NOT INSTALLED** | none | Loopback-only |
| `cli/main.py` | doctor/run/simulate/replay/… | Python | VERIFIED_WORKING | manual | No arm command |
| `fixtures/testbed/` | Reference app | Python | VERIFIED_WORKING | — | Ground truth |
| **GUI** | — | — | **NOT STARTED** | — | Required for arming |
| **UI Automation** | — | — | **NOT IMPLEMENTED** | — | P7 |

---

# Part 4 — Feature inventory

| Area | Feature | Status | Evidence | Tests | Safe now? | Next action |
|---|---|---|---|---|---|---|
| Startup | CLI entry (`frameforge`) | VERIFIED_WORKING | `cli/main.py` | manual | yes | — |
| Startup | GUI startup | NOT_STARTED | no `gui/` dir | none | n/a | Build after Phase C |
| Config | pydantic Settings | VERIFIED_WORKING | `config/settings.py` | unit | yes | — |
| Config | Env/secret handling | VERIFIED_WORKING | redactor, 10 shapes | unit | yes | — |
| Logging | Append-only redacted log | VERIFIED_WORKING | `kernel/bus.py` | unit | yes | — |
| Errors | Structured `BlockResult` | VERIFIED_WORKING | `actions/arm.py` | 28 | yes | — |
| Scenarios | JSON/YAML load + validate | VERIFIED_WORKING | `tasks/loader.py` | integration | yes | — |
| Scenarios | Setup steps (best-effort) | IMPLEMENTED_UNVERIFIED | `tasks/dsl.py` | none | no | Live-verified with arm |
| Actions | Schema requires `at` | VERIFIED_WORKING | import check | unit | yes | — |
| Actions | Compiler refuses point-less | VERIFIED_WORKING | `_require_point` | unit | yes | — |
| Planner | Tier-0 deterministic | VERIFIED_WORKING | `planning/tier0.py` | unit | yes | — |
| Planner | Tier-1 AI (CLI bridge) | IMPLEMENTED_UNVERIFIED | `scripts/probe_ai.py` | unit | no | Needs live gate |
| Planner | Planner boundary (actions only) | VERIFIED_WORKING | no OS calls in planning | unit | yes | — |
| Compiler | Action→primitives | VERIFIED_WORKING | `actions/compiler.py` | unit | yes | — |
| Input (mock) | MockInputController | VERIFIED_WORKING | 28 tests | unit | yes | — |
| Input (live) | SendInput backend | **BROKEN — bypass** | `scripts/*` call it raw | — | **NO** | Delete/quarantine scripts |
| Input | Key state tracking | VERIFIED_WORKING | `HoldTracker` | unit | yes | — |
| Input | Mouse button tracking | VERIFIED_WORKING | `HeldItem` | unit | yes | — |
| Input | Hold deadline watchdog | VERIFIED_WORKING | `enforce_deadlines` | unit | yes | — |
| Safety | Release never blocked by disarm | VERIFIED_WORKING | `is_release` | 40 unit | yes | — |
| Safety | Defensive modifier sweep | VERIFIED_WORKING | `release_all` | 40 unit | yes | — |
| Safety | Hardened child env | VERIFIED_WORKING | 8 spawn sites | CI test | yes | — |
| Safety | Context-menu key refused | VERIFIED_WORKING | `MENU_TRIGGER_KEYS` | unit | yes | — |
| Safety | Right-click denied by default | VERIFIED_WORKING | `RIGHT_CLICK` | unit | yes | — |
| Stop | Emergency stop, 3 paths | VERIFIED_WORKING | 16 ms measured | hardware | yes | — |
| Recovery | `recover-input` | VERIFIED_WORKING | CLI run | manual | yes | — |
| Deny-list | Class/process deny-list | VERIFIED_WORKING | 10 classes, 39 procs | unit | yes | — |
| Registry | Protected-window registry (PID/HWND) | **PARTIAL** | `ProtectedRegistry` exists | unit | **NO** | Not instantiated at runtime |
| Target | Session registration | **PARTIAL** | `TargetSession` exists | 11 | **NO** | **Not wired (Blocker 1)** |
| Target | HWND/PID/exe verification | **PARTIAL** | `evaluate_runtime_conditions` | 28 | **NO** | Same |
| Target | `WindowFromPoint` pre-move | **PARTIAL** | executor calls guard | unit | **NO** | No guard supplied at runtime |
| Target | `WindowFromPoint` pre-button | **PARTIAL** | executor calls guard | unit | **NO** | Same |
| Target | Foreground validation | VERIFIED_WORKING | policy + guard | 28 | yes | — |
| Capture | Target-scoped capture | **PARTIAL** | `TargetCapture` exists | none | **NO** | Not wired |
| Capture | Freshness enforcement | **PARTIAL** | `CaptureFrame.stale` | unit | **NO** | Same |
| Vision | VisionProposal (cannot inject) | VERIFIED_WORKING | type has no send | unit | yes | — |
| Vision | UI Automation | **NOT_IMPLEMENTED** | no uiautomation dep | none | n/a | Phase E |
| Gameplay | Gameplay-state verifier | **DESIGNED_ONLY** | `GameplayVerdict` enum | 28 | **NO** | Needs a real detector |
| Arm | Execution state machine | **NOT WIRED** | 28 tests pass | 28 | **NO** | **Blocker 1** |
| Arm | Input profiles | VERIFIED_WORKING | movement-only default | unit | yes | — |
| Arm | Countdown | **NOT WIRED** | `begin_countdown` | 28 | **NO** | Same |
| Arm | `SYSTEM_INTENTS` unopt-outable | VERIFIED_WORKING | `permits()` | unit | yes | — |
| Monitor | Virtual-desktop coords | VERIFIED_WORKING | unit tests | unit | yes | — |
| Monitor | Negative origins | VERIFIED_WORKING | unit test | unit | yes | — |
| Monitor | DPI awareness | **PARTIAL** | `enable_per_monitor_dpi()` exists | hardware | **NO** | Never called at startup |
| Monitor | Topology-change detection | **PARTIAL** | fingerprint exists | unit | **NO** | Not consulted at runtime |
| Report | report.json/.md | VERIFIED_WORKING | `qa/report.py` | integration | yes | — |
| Report | Input-state section | VERIFIED_WORKING | `input_safety` | integration | yes | — |
| Report | First-divergence analysis | VERIFIED_WORKING | canary run | integration | yes | — |
| Tests | Headless suite | VERIFIED_WORKING | 511 pass | — | yes | — |
| Tests | Hardware suite | VERIFIED_WORKING | 33 pass, 1 skip | — | guarded | — |
| Tests | Injection fuzz gate | VERIFIED_WORKING | 20 texts + 10 rationales | unit | yes | — |
| Build | VCS | **NOT_STARTED** | no `.git` | — | n/a | `git init` |
| Build | CI | **NOT_STARTED** | none | — | n/a | Phase B |
| Build | Lint/typecheck | **NOT_STARTED** | ruff configured, not installed | — | n/a | Phase B |
| Build | Packaging (wheel/sdist) | **NOT_STARTED** | editable only | — | n/a | Phase D |

---

# Part 5 — Input-safety audit

## Injection call sites (AST-verified: call, not reference)

| File:line | API | Bypasses safety mgr | Bypasses TargetGuard | Cursor-affecting | No explicit point | Fixed coord | Validates HWND/PID | WindowFromPoint | Foreground | Protected win | Cleanup | Mock/Live | Severity | Remediation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `actions/safety.py:86,87,1034,1102,1139` | `SendInput` | **no — this IS the mgr** | delegated | yes | no (validated) | no | via guard | yes | yes | yes | yes | live, approved | — | none |
| `scripts/probe_estop_hook.py:101,102,105` | `SendInput` | **YES** | **NO** | yes | **YES** | no | **NO** | **NO** | **NO** | **NO** | none | **LIVE** | **CRITICAL** | **Delete or quarantine** |
| `scripts/probe_input_timing.py:37,57` | `SendInput` | **YES** | **NO** | yes | **YES** | **YES** (300,300) | **NO** | **NO** | **NO** | **NO** | none | **LIVE** | **CRITICAL** | **Delete or quarantine** |
| `tests/hardware/test_hil.py` (injection classes) | via port | no | guarded | yes | no | **NONE** | n/a | n/a | n/a | n/a | yes | guarded/live | — | none |
| `fixtures/testbed/*` | **NONE** | — | — | — | — | — | — | — | — | — | — | — | Test app, injects nothing |

**No `pyautogui`, `pynput`, `keyboard`, `mouse`, AutoHotkey, `SetCursorPos`,
`mouse_event` call exists anywhere.** Matches in test files are assertions *banning* them.

Two residual `keybd_event` references in `adapters/window/pywin32_window.py:177,416` —
verified as **argtypes declarations only, never invoked** (they are the Windows-Hook
declaration for the estop hotkey; the ALT-tap that caused the earlier IME corruption was
removed).

## P0–P9 status

| Item | Status | Evidence |
|---|---|---|
| **P0** no locationless click | **COMPLETE_AND_VERIFIED** | Schema rejects (import check), compiler `_require_point`, runtime `denied` + zero emissions. 3 layers, all asserted. |
| **P0.5** fixed coordinates removed | **COMPLETE_AND_VERIFIED** in `tests/` (**NONE** found) · **BROKEN** in `scripts/` (`probe_input_timing.py` uses `(300,300)`) | grep over `tests/` |
| **P1** TestTargetSession mandatory | **IMPLEMENTED_NOT_VERIFIED** | Class + 19 fields; **runner/CLI never construct it** |
| **P2** WindowFromPoint before move + before button | **IMPLEMENTED_NOT_VERIFIED** | Executor calls `validate_point` twice; **no guard is supplied at runtime** |
| **P3** Protected-window registry | **PARTIAL** | Class exists, PID/HWND-based; **never instantiated in the run path** |
| **P4** Named coordinate types + DPI | **IMPLEMENTED_NOT_VERIFIED** | Types used in compiler/executor; `enable_per_monitor_dpi()` exists but **is never called at startup** |
| **P5** Topology-change invalidation | **IMPLEMENTED_NOT_VERIFIED** | Fingerprint computed; **never consulted on the live path** |
| **P6** Target-scoped capture + vision | **IMPLEMENTED_NOT_VERIFIED** | `TargetCapture`, `VisionProposal` (provably cannot inject); **not wired** |
| **P7** UI Automation | **NOT_IMPLEMENTED** | no dependency; targeting is coordinate-only |
| **P8** User-armed state machine | **IMPLEMENTED_NOT_VERIFIED** | 28 tests pass in isolation; **never constructed by runner/CLI; no GUI to arm it** |
| **P9** Emergency stop + cleanup | **COMPLETE_AND_VERIFIED** | 16 ms steady state, 3 paths, defensive sweep asserted |

**Not one of P1–P6, P8 is verified end-to-end.** The component tests are real; the
integration is absent.

---

# Part 6 — Known defects and risk register

| ID | Sev | Area | Description | Trigger | Impact | Mitigation | Root cause | Fix | Blocking |
|---|---|---|---|---|---|---|---|---|---|
| D1 | **CRITICAL** | Safety | Arm/TargetSession not wired | any live run | All safety gates bypassable | None | Features added, not integrated | Wire in runner + CLI | **YES** |
| D2 | **CRITICAL** | Safety | `scripts/probe_estop_hook.py`, `probe_input_timing.py` inject raw | running either script | Corruption of user input; wrong-window clicks | None | Written as diagnostics before the gate existed | Delete or quarantine behind an explicit flag | **YES** |
| D3 | HIGH | Repro | No git repository | — | No history/rollback/attribution | None | Never initialised | `git init`, first commit | Yes, for Phase B |
| D4 | HIGH | Perception | WinRT OCR recall degraded on small dark body text | Notepad run | Scenario fails; report blames perception for a capability failure | Documented; backend-availability test added | Undetermined (engine-dependent) | Try RapidOCR fallback; separate capability failure from product failure | No |
| D5 | HIGH | Targeting | No UI Automation | targeting any app | Coordinate-only; brittle | — | Deferred | Phase E | No |
| D6 | MEDIUM | Safety | `TargetGuard` not supplied at runtime | live run | Pre-move/post-button checks never run | — | Same as D1 | Same as D1 | **YES** |
| D7 | MEDIUM | Env | Single monitor connected | now | Dual-monitor acceptance cannot run | — | Environment, not code | Reconnect second display | No |
| D8 | MEDIUM | UX | No GUI | arming | Cannot arm without CLI plumbing | — | Not started | Phase C | Yes, for real use |
| D9 | LOW | Notepad | Scenario red (8/12 at last live run) | — | No green third-party proof | — | OCR recall + scenario postconditions | Fix D4, re-run | No |
| D10 | LOW | Build | No linter/typechecker in CI | — | Regressions caught late | ruff config present, unused | Not wired | Add ruff+mypy, run in CI | No |
| D11 | LOW | Repro | 128 installed vs 20 pinned | fresh install | Drift | lockfile present | Transitives unpinned | `pip freeze` full lock | No |
| D12 | INFO | Perf | Capture ~108 ms/frame | every observation | ~9 fps ceiling | Measured, documented | BitBlt on i5-8250U | DXGI promotion | No |

### Historical defects — closed, with evidence

| Defect | Status | Evidence |
|---|---|---|
| Stuck Ctrl+Shift / keyboard corruption | **CLOSED** | Cause: untracked raw-VK injection that refused releases once disarmed. Fixed: single manager, release never blocked, 40 tests. Not reproducible in 511 tests. |
| Unexpected layout/IME switch | **CLOSED** | Cause: ALT keybd_event tap in `set_foreground`. Removed; zero `keybd_event` calls remain. Layout restored to en-US manually; `recover-input` automates it. |
| FxSound output-device switching | **CLOSED (root cause)** | Was a symptom of stuck Ctrl+Shift. |
| Spontaneous context menus | **CLOSED** | Cause: `test_all_buttons_round_trip` right-clicked a fixed primary-monitor point with Hermes foreground. Test deleted; no fixed points remain. |
| Clicks landing in Hermes | **MITIGATED, not closed** | TargetGuard exists and is tested with a mock backend; **not wired**, so the live path is still exposed (D1). |
| Emergency-stop reliability | **CLOSED** | 3 paths; hook object retained; 16 ms; measured over 5 runs. |
| Cancellation/timeout cleanup | **CLOSED** | `finally` in runner and director; 12 exit-path tests. |

**Honest note:** "CLOSED" means the specific cause is fixed and no longer reproduces. The
class of defect is *mitigated* by architecture, not eliminated — a live run remains
possible only through a path that does not yet exist (D1). Until D1 is fixed, no live run
should be attempted at all.

---

# Part 7 — Test and validation status

| Suite | Count | Last result | Backend | Environment | Notes |
|---|---|---|---|---|---|
| `tests/unit` | 491 | **511 pass** (with integration) | mock | headless | Includes 40 input-safety, 28 state-machine, 11 acceptance, 13 budget, 20 fuzz |
| `tests/integration` | 20 | included above | mock + fakes | headless | Real director, faked OS |
| `tests/hardware` | 34 | **33 pass, 1 skip** | live capture/OCR; **injection guarded** | **user desktop** | Skip reason: "refusing to send a global chord with a protected window in front" |
| Lint / typecheck | — | **NOT CONFIGURED** | — | — | — |
| CI | — | **NONE** | — | — | — |
| Manual / live | — | **NOT RUN — UNSAFE / BLOCKED** | live input | user desktop | Prohibited by this audit |

Environment classification: 511 headless tests use **mock input**. The 33 hardware tests
use **live capture and OCR on the user desktop** but their *injection* is guarded and
currently skipping. **No test uses UI Automation** (not implemented). **No test uses an
isolated VM** (none exists).

### Release-gate matrix

| Gate | Requirement | Status | Evidence | Blocker |
|---|---|---|---|---|
| No locationless click | enforced 3× | **PASS** | import + unit | — |
| No unsafe fixed screen point | none in `tests/` | **PARTIAL** | `scripts/probe_input_timing.py` still has one | **YES** |
| No direct live input outside backend | one module | **FAIL** | 2 scripts call raw `SendInput` | **YES** |
| Valid target session required | mandatory | **FAIL** | never constructed at runtime | **YES** |
| Verified point ownership required | `WindowFromPoint` ×2 | **FAIL (untested live)** | guard not supplied | **YES** |
| Protected-window denial required | PID/HWND | **FAIL (untested live)** | registry not instantiated | **YES** |
| Focus loss → immediate cleanup | pause + release | **PASS (mock)** | 28 state-machine tests | — |
| Cancellation/timeout → cleanup | `finally` | **PASS (mock)** | 12 exit-path tests | — |
| Emergency stop verified | <100 ms | **PASS** | 16 ms, 5 runs | — |
| Keyboard/mouse clean after run | sweep asserted | **PASS (mock)** | 40 tests | — |
| Dual-monitor safety validated | Hermes + external | **NOT RUN — BLOCKED** | second display disconnected | **YES** |
| DPI/topology validated | change cancels | **PARTIAL** | fingerprint exists, not wired | — |
| Target capture freshness validated | stale → block | **PARTIAL** | unit only, not wired | — |
| User-arm state machine validated | ACTIVE sole state | **PASS (mock)** | 28 tests | — |
| High-severity defects resolved | D1, D2 | **FAIL** | open | **YES** |

**Four gates fail. The project is not clear for live input.**

---

# Part 8 — UI readiness

### Safe to begin now

Non-interactive scaffolding that cannot emit input and does not imply the engine is ready:
a static status/diagnostic view reading the audit log, report schema, and test results. The
existing `api/server.py` can back it once `fastapi` is installed.

### Must wait

Anything that displays "ARMED", "ACTIVE", target identity, or a countdown. Those controls
would represent safety state the run path does not yet enforce — a green "ACTIVE" badge over
an unguarded engine is worse than no badge.

### Required panels before any game testing

| Panel | Criticality | Note |
|---|---|---|
| Target selection | **Safety-critical** | Must show HWND/PID/executable, not just a title |
| Target preview (bound to session) | **Safety-critical** | Preview is not permission |
| Target identity display | **Safety-critical** | Include image path and PID |
| INPUT LOCKED / ARMED / ACTIVE banner | **Safety-critical** | Must come from the real state machine |
| Countdown with cancel | **Safety-critical** | |
| Pause / Emergency stop | **Safety-critical** | Not a keyboard shortcut that can collide |
| Safety-failure explanation | **Safety-critical** | Show the exact `BlockReason` |
| Audit/event log view | Useful | Already available as JSONL |
| Profile selection | Useful | |
| Gameplay confirmation | **Required for arming** | Cannot be self-asserted |

### Cosmetic vs safety-critical

Cosmetic: theming, layout, log formatting, colour scheme, icons, graphs.

Safety-critical: every panel above marked **safety-critical**. None may be visually
de-emphasised, and none may show a permissive default.

### Recommended order

1. Read-only status/log view (no input controls).
2. Target selection + identity + preview — **but read-only, with no ARM button yet**.
3. *After* D1 and D2 are fixed: the arm/countdown/banner controls.
4. Then general usability.

Step 2 before step 3 is deliberate: it lets the hardest UI work proceed while keeping the
arm control absent until the engine enforces arming.

---

# Part 9 — Game-testing readiness

| # | Target | Status | Basis |
|---|---|---|---|
| 1 | Mock-only scenario testing | **READY** | 511 tests; fakes satisfy the real port contract |
| 2 | Notepad / standard app, isolated | **CONDITIONALLY_READY** | Component gates pass in isolation; **blocked on D1, D2** and a green Notepad run (D4/D9) |
| 3 | Offline / single-player game, isolated | **NOT_READY** | No UI Automation; no gameplay verifier; single monitor |
| 4 | Local / private controlled game | **NOT_READY** | Same |
| 5 | Official online multiplayer | **NOT_RECOMMENDED** and **NOT APPROVED** | See below |

### GTA V Online / official Rockstar servers

- **NOT APPROVED FOR AUTOMATED TESTING.**
- No live bot or automation testing.
- No testing intended to bypass or evade anti-cheat.
- Do not disable, interfere with, probe, reverse-engineer or work around anti-cheat.
- No automation for progress, rewards, competitive advantage, unattended play, mission
  grinding, or any other online activity.
- Requires a separate legal / terms / publisher-permission review before any future
  consideration.

Automating a human-visible input stream against a competitive service is detectable in
principle, and treating it as undetectable would be both wrong and a terms violation.

**First real-game target, if needed:** an offline single-player title, a local sandbox, or
a purpose-built test scene — **not** an official online service.

---

# Part 10 — Completion roadmap

### Phase A — Critical safety blockers

| Task | Goal | Modules | Depends on | Acceptance | Tests | Safety | Size | Blocks UI | Blocks isolated | Blocks all game |
|---|---|---|---|---|---|---|---|---|---|---|
| A1 | Delete or quarantine the two bypassing scripts | `scripts/` | — | Zero `SendInput` calls outside `actions/safety.py` (AST) | static CI check | **S** | S | no | **YES** | **YES** |
| A2 | Wire `ExecutionStateMachine` into the runner | `tasks/runner.py`, `actions/arm.py` | A1 | A run cannot reach ACTIVE without an arm token; no arm → no input | integration with fake machine | **S** | M | no | **YES** | **YES** |
| A3 | Construct `TargetSession` from the resolved window and pass a real `TargetGuard` to the executor | `tasks/runner.py`, `actions/target.py` | A2 | Live-path guard runs; refusal recorded | integration | **S** | M | no | **YES** | **YES** |
| A4 | Instantiate `ProtectedRegistry` for the agent's own process at startup | `actions/target.py`, `kernel` | A3 | Agent pid/hwnds refused regardless of title | unit + integration | **S** | S | no | **YES** | **YES** |
| A5 | `git init` + first commit | repo | — | Reproducible build identity | — | I | S | no | no | no |
| A6 | Add ruff + mypy and run them | `pyproject.toml` | A5 | Clean run recorded | CI | I | S | no | no | no |

### Phase B — Core completion and verification

| Task | Modules | Acceptance | Size | Blocks |
|---|---|---|---|---|
| B1 | Call `enable_per_monitor_dpi()` at startup; assert it took effect | **M** | isolated |
| B2 | Consult the topology fingerprint on the live path; cancel pending input on change | **M** | isolated |
| B3 | Wire `TargetCapture` + freshness into the runner | **M** | isolated |
| B4 | Recovery ladder wired and tested end-to-end | **M** | isolated |
| B5 | Full lockfile (all transitive pins) | **S** | no |
| B6 | CI running headless tests + ruff + mypy | **M** | no |

### Phase C — Safety-critical UI

Target selection → identity → preview → arm/countdown/banner → pause/estop → failure
explanation. **Depends on A2/A3 completing**, so the UI reflects real enforcement.
**L**, blocks all game testing.

### Phase D — General UI / usability

Theming, layout, log views, profile editing. **M**. Cosmetic.

### Phase E — Mock and isolated test scenarios

UI Automation integration (prefer element targeting over coordinates); a real
gameplay-state verifier; Notepad scenario green on the second monitor with Hermes visible.
**L**. Blocks offline game validation.

### Phase F — Offline / local game validation

First purpose-built or offline target, isolated environment, user-armed. **L**.
Depends on all of A–E.

### Phase G — Deferred

Multi-agent workspace; remote executor; plugin architecture; voice; MCP; broader
provider support. Not to be started.

---

# Part 11 — Final recommendation

## CURRENT BUILD DECISION

**A. Continue safety/input completion before UI work.**

D. Safe for mock-only scenario work is *also* true today, but A is the active priority —
mock work should be aimed at closing A1–A4, not at new scenarios.

### Next single highest-value implementation task

**A2 — wire `ExecutionStateMachine` into the runner so a run cannot reach ACTIVE without a
user arm token.** It is the smallest change that makes the other gates meaningful, because
until then none of them are consulted.

### Next single highest-value verification task

**A1 + A3 together:** delete the two bypassing scripts, then run the Notepad POC on the
second monitor with Hermes visible and the pointer deliberately left over Hermes —
asserting exactly one right-click inside Notepad and zero events anywhere else. That is
the one test that would move most gates from untested to verified.

### Next UI task safe to begin

**A read-only status and audit-log view** showing run state, input-safety health and the
event log. No target selection, no ARM control, no countdown — those would represent safety
state the engine does not yet enforce.

### Exact conditions before any live game test is allowed

1. `git` initialised and a clean commit exists.
2. Zero `SendInput` calls outside `actions/safety.py`, enforced by a CI check.
3. A run cannot reach ACTIVE without a user arm token bound to a live `TargetSession`.
4. The live path constructs a `TargetSession` and supplies a `TargetGuard`, with
   `WindowFromPoint` verified before movement and before button-down.
5. The agent's own pid/hwnds are in a `ProtectedRegistry` for the whole run.
6. Post-run verification proves no key/button held, no layout change, no worker or hook
   alive.
7. Dual-monitor acceptance passed with Hermes visible.
8. Emergency stop verified twice on the live path, under 100 ms.
9. UI Automation available and used for element targeting.
10. A real gameplay-state verifier returning `ACTIVE_GAMEPLAY`.
11. Offline/single-player target only, in an isolated environment, with explicit user
    approval per run.

**Do not begin broad game automation until all eleven hold.** Not one currently does.
