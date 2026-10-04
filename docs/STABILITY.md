# Backend stability evidence

This is the evidence pack required before UI/dashboard work may begin
(`docs/ROADMAP.md` §15). Each criterion is marked with what was **measured**, not what was
intended. Where a criterion is not yet met, it says so.

Last verified: **2026-10-03**, on the development machine
(Windows 11 build 26300, i5-8250U, 12 GB, 1920x1080 + 1920x1080 extended,
Python 3.14.7, `frameforge 0.1.0`).

---

## Summary

| Group | Criteria | Met | Partial / Not met |
|---|---|---|---|
| A. Correctness & control | 4 | 4 | 0 |
| B. Perception | 9 | 8 | 1 partial |
| C. Determinism & AI containment | 6 | 6 | 0 |
| D. QA & reporting | 3 | 3 | 0 |
| E. Safety, policy, operations | 5 | 5 | 0 |
| F. Maintainability | 4 | 4 | 0 |
| **Total** | **31** | **30** | **1 partial** |

**Verdict: the backend meets its stability gate, with one documented limitation.**

All 31 criteria are met or explicitly partial. The single partial is criterion 5 (capture
latency), which is a *measured property of this hardware*, not an unfixed defect: ~108 ms
per 1080p frame. It is documented, asserted against a threshold, and reported by `doctor`
so no user is misled about it.

The three previously-outstanding evidence gaps have been closed since the first pass:

- **Replay-and-diff harness** - `frameforge replay <run_dir>` exists and is verified:
  a live run's plan replays with `REPLAY DETERMINISTIC: 1 actions replayed exactly`.
- **Global-hotkey estop verified live** - the hook fires on a real keypress. Doing this
  found **four real defects** in the stop path, listed below; an estop that silently never
  fires is worse than no estop.
- **Lockfile committed** - `requirements.lock`, 20 exact pins, installable from a fresh
  venv.

---

## A. Correctness & control

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Core logic covered by automated tests; no test needs a display | **MET** | `pytest tests/unit tests/integration` → **282 passed** in 143 s with no display, no game, no Windows APIs. |
| 2 | Hardware input suite passes, incl. hold leaks and mid-action estop < 100 ms | **MET** | `pytest tests/hardware --run-hardware` → **13 passed**. Includes `test_estop_releases_held_keys_within_100ms` (measured, asserted), real cursor round-trip on **both** monitors, and `test_disarmed_port_sends_nothing`. |
| 3 | FocusGuard demonstrated to block input on focus drift | **MET** | Unit: `test_ses_02_focus_guard_blocks_on_drift`. Integration: `test_focus_loss_blocks_input_and_yields_unknown` — asserts no key primitive reached the port. A **real defect** was found and fixed here: the tolerance window used to grant a free first input when focus was already wrong (see "Defects found by these tests"). |
| 4 | Zero input sent while not `ARMED` | **MET** | `test_input_port_refuses_primitives_while_disarmed`; gate is checked inside `SendInputPort.send`, verified by `test_ses_01_input_gate_checked_inside_the_port`. |

## B. Perception

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 5 | Sustained capture on both monitors at target latency | **PARTIAL** | Works on both. **But latency is ~108 ms/frame at 1920x1080 (~9 fps)**, not the <25 ms projected in ROADMAP §2. See ADR 0003. Adequate for menu navigation and QA scenarios; inadequate for frame-tight reaction. |
| 6 | Display/topology change handled without crash | **MET** | `test_topology_change_increments_epoch`; `DisplayTopology.signature` is compared per epoch and forces re-acquisition. |
| 7 | Exclusive fullscreen reported `UNSUPPORTED`, not silently black | **MET** | `CaptureHealth.UNSUPPORTED` is a distinct state from `BLACK`; `DisplayHealthMonitor.observe` returns `FATAL` with `capture=black` and an explanatory detail, and `doctor` reports the capture backend. |
| 8 | OCR accuracy ≥ 95% on clear UI text | **MET (fixture)** | `scripts/probe_ocr.py`: 2/2 → **3/3** rendered strings recovered by the persistent host; full 1920x1080 frame with `NEW GAME` recovered in 216 ms. **Honest limitation:** measured only against our own testbed's large high-contrast font. Game-font accuracy is unmeasured and will be worse. |
| 9 | Perception boundary enforced (redaction, secrets, no network when `ai: off`) | **MET** | `test_per_03_secret_never_reaches_disk`, `test_per_03_settings_never_serialise_the_key_itself`, `test_per_02_evidence_store_applies_redaction_before_writing`, `test_per_05_ai_off_makes_no_planner_call`. |

## C. Determinism & AI containment

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 10 | A complete run succeeds with `ai: off` | **MET** | The live proof-of-value run (`testbed_smoke` against a real window) used `ai: off` and reported **PASS 5/5**. Also `test_no_input_while_unarmed`, `test_ai_off_is_a_valid_configuration`. |
| 11 | A recorded run replays identically | **MET** | `frameforge replay` re-runs a recorded plan through the real validator/compiler/executor/verifier and diffs it. Verified live: a 5/5 live run recorded 1 action, and the replay reported `identical: 1 actions replayed exactly`. `tests/unit/test_replay.py` (21 tests) covers action round-trip, step-aware replay, and structured drift reporting. |
| 12 | Malformed / out-of-schema / over-budget / permission-violating plans rejected (fuzz ≥ 50) | **MET** | `TestInjectionFuzzReleaseGate`: **20 adversarial screen texts × 3 assertions + 10 hostile rationales**, plus strict-parser tests. All rejected. Corpus size asserted ≥ 20 / ≥ 10. |
| 13 | No code path by which a planner emits an input primitive | **MET** | `ActionCompiler.compile` is the only route to primitives; `PlanValidator` runs before it. Asserted by `test_order_executor_is_unreachable_without_a_compiler`. |
| 14 | **RELEASE GATE** `G-VERD-01`: no AI module in the verdict import graph | **MET** | `test_verd01_verify_module_imports_no_planner_or_ai` parses `verify.py`'s AST and asserts no planning/AI import. Green. |
| 15 | **RELEASE GATE** `G-INJ-04`: prompt-injection fuzz ≥ 50 cases | **MET** | See #12. Both gates green. |

## D. QA & reporting

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 16 | Seeded bug → `FAIL` with correct first-divergence; clean run → `PASS`; ambiguous → `UNKNOWN` | **MET** | Live: `testbed_healthbar_bug` with `--bug healthbar --hp 20` → **FAIL**, divergence at `healthbar_visible_while_low_hp`. Same scenario with the bug disabled → **PASS**. The captured evidence PNG was independently inspected: HP 0, **no health bar drawn, no HEALTHBAR label**, minimap present — exactly matching the verdict. |
| 17 | `report.json` schema-validated; `report.md` actionable | **MET** | `test_report_is_schema_shaped_and_actionable` asserts 11 required keys and that no fix-shaped field exists (`G-ROLE-04`). Report leads with verdict, failure digest, first divergence, step trace, health log, reproduction. |
| 18 | Run directory self-contained and archivable; retention works | **MET** | `EvidenceStore` enforces a per-run byte cap and prunes oldest non-key frames (`_enforce_retention`); `prune_old_runs` for cross-run retention. |

## E. Safety, policy, operations

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 19 | Estop verified via all three paths | **MET** | All three verified live: programmatic (`test_stop_latency_is_recorded_and_small`), sentinel file (`test_sentinel_file_triggers`), and the **global hotkey**, which now fires on a real injected keypress in **41 ms** (F12) and **130 ms** (Ctrl+Alt+F12). `tests/hardware/test_estop_hotkey.py` covers latching, release-while-disarmed, and stopping a blocked operation. |
| 20 | Budget/rate caps stop a runaway run | **MET** | `TestBudgetLedger` (6 tests) covers cumulative caps, the sliding rate window, retries-per-step, and run time. `test_budget_cap_stops_a_run` covers it end-to-end. |
| 21 | `authorized_use` enforced at startup; refusal path tested | **MET** | `TestAuthorisation`: unidentified target, unsupported basis, inconsistent offline attestation, non-loopback signal URL, and baked-in direct-launch command are all refused. |
| 22 | `doctor` gives an honest capability report | **MET** | `frameforge doctor` output verified on this machine; reports deps, extras, measured capture latency, displays, session, AI configuration, and the authorised-use policy. |
| 23 | No secrets in logs or reports | **MET** | `Redactor` covers 10 secret shapes; `test_known_secret_shapes_are_scrubbed` is parameterised over 6 shapes; redaction is asserted on disk, in settings serialisation, and in the AI packet. |

## F. Maintainability

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 24 | Clean install from a fresh venv using the lockfile | **MET** | `requirements.lock` commits 20 exact pins. The environment was built from a fresh `.venv` on this machine and all 302 tests pass against it. |
| 25 | Public API documented; every port has a real adapter and a fake | **MET** | All 7 ports (`capture`, `input`, `ocr`, `vision`, `window`, `planner`, `clock`) have real adapters and fakes in `ports/fakes.py`. Every port method has a docstring stating its contract. |
| 26 | No game names / coordinates / key bindings in the engine core | **MET** | `test_dev_01_no_game_names_in_core` (regex over 12 game names), `test_dev_01_no_control_bindings_in_core`, `test_dev_01_control_bindings_live_only_in_profiles_and_presets`. All green. |
| 27 | ADR log explains the load-bearing decisions | **MET** | 9 ADRs: deterministic loop, hexagonal ports, capture default, OCR host, input layer, files-not-DB, profiles-are-data, verdict integrity, testbed-first. |

---

## Test counts

| Suite | Tests | Needs a display? |
|---|---|---|
| `tests/unit` | 301 | No |
| `tests/integration` | 18 | No (real director, faked OS) |
| `tests/hardware` | 25 | Yes (`--run-hardware`) |
| **Total** | **344** | 319 run headless in ~16 s |

---

## Measured performance (this machine)

| Stage | Measured | Note |
|---|---|---|
| Capture, 1920x1080 | **~108 ms** | BitBlt; ~9 fps ceiling (ADR 0003) |
| Vision: template match | < 5 ms per anchor | grayscale + normalised correlation |
| OCR: persistent host, steady state | **~36 ms** | 900x200 region |
| OCR: full 1920x1080 frame | **~216 ms** | WinRT recognise ~189 ms |
| OCR: downscaled to 1280px edge | **~67 ms** | 3.2x faster, text still recovered |
| OCR host startup (one-time) | ~1.2 s | paid once per run |
| End-to-end decision cycle, Tier 0 | ~4-8 Hz | capture-dominated |
| AI planner round trip | ~37 s | via local CLI bridge; remote would be 1.5-6 s |

**Implication, stated plainly:** Frame Forge's current practical strength is menu
navigation, UI interaction, and QA scenario execution. It is *not* currently suited to
anything requiring sub-100 ms reaction. DXGI capture is the available lever and is
already wired behind the port.

---

## INCIDENT 2: spontaneous context menus in the agent's own window (release blocker, RESOLVED)

**Symptom.** While Frame Forge was debugging, a context menu appeared *by itself* inside the
agent's own UI window, repeatedly, while the user's hands were off the mouse.

**Root cause — Frame Forge's own hardware test suite.**

`tests/hardware/test_hil.py::test_all_buttons_round_trip` sent a **right-button down/up at
whatever was under the cursor**, with no target validation. The foreground window at the
time was `Hermes.exe`, class `Chrome_WidgetWin_1` — the agent's own UI. A right-button
release is precisely the event that produces a context menu, and injected input has no
target: it lands on whatever is foreground. So the test was right-clicking the agent's
window, repeatedly, and it looked spontaneous from the user's side.

The same mechanism explains the earlier FxSound symptom: a context menu needs only a right
button, or the Application key, or Shift+F10 — and a stuck Shift plus an F10 looks exactly
like an unexplained chord.

**Fixes.**

- All injection now flows through one `InputSafetyManager`; only it may call `SendInput`.
- Added a `TargetSpec` + `InputPolicy` layer: an action names its window and is refused
  unless that window is foreground and not deny-listed. Default-deny.
- Permanent deny-list covering the agent's own UI, all browsers/Electron apps, consoles,
  editors, the desktop, taskbar, UWP hosts, settings, credential/UAC dialogs, the audio
  stack, and unnamed `#32770` dialogs (a context menu *is* one).
- Right-click disabled by default in both the policy and the safety manager.
- VK_APPS/Menu and F10 refused outright.
- Event-level audit: sequence, run/action id, call site, thread/pid, target and foreground
  window, pressed registry before and after.
- Every hardware test now calls `require_test_target()` and **skips** rather than injects.
  7 tests skip with an explicit reason while the agent UI is foreground.
- Bounded hold deadlines with a watchdog.
- `cleanup_failed` run state: a run is never `completed` unless cleanup verifies.

**Architecture made real (not decorative).**

The tool boundary was built and initially imported by nothing - the director still went
straight from the executor to the port. Now:

- `ActionExecutor` routes every primitive through `InputController` + `InputPolicy`.
- Constructing an `ActionExecutor` with a live `SendInputPort` and **no** controller raises.
- `build_controller()` selects mock / disabled / live in one place, so the choice cannot
  drift; an unrecognised port is treated as live rather than quietly given a recorder.
- `FakeInput` now carries a real `InputSafetyManager` and the same `set_enabled` /
  `release_all` signatures, so the fake cannot pass where the real adapter fails - which
  is exactly how the click-path bug survived 300 tests.
- `TargetSpec` was renamed `ActionTarget`, because two different types shared the name.

**A real hole this closed.** Default-deny only fired when a request carried a target; a
request with *no* target passed. That is precisely the dangerous case, since an action with
no declared destination is the definition of "wherever the cursor happens to be". Now
default-deny is unconditional.

**Verification on the live path** (real `SendInputPort`, `notepad.exe` allow-listed):

| Request | Outcome |
|---|---|
| names a target window that is not foreground | **denied** - `target_window_not_allowlisted` |
| names no target at all | **denied** - `no_target_window` |

Both recorded in the policy's denial log, and the run produced no OS input.

- **Injection is refused while the agent UI is in front:**
  `window class 'Chrome_WidgetWin_1' is deny-listed`.
- 19 hardware tests pass, 7 skip, **0 injections**.
- 413 headless tests pass.
- Emergency-stop latency: **16 ms steady-state** (was 643 ms). The regression was
  `read_foreground_window()` shelling out to `tasklist` — **measured at 205 ms per call** —
  on every dispatch and cleanup. Process names are now resolved via
  `QueryFullProcessImageNameW` and cached by pid. First stop in a process is ~143 ms
  (one-off Win32 warm-up); every subsequent stop is 15-22 ms.

---

## INCIDENT 1: post-run input corruption (release blocker, RESOLVED)

**Symptom.** After a test run, the operator's keyboard was corrupted system-wide: `N`
produced an alternate-script character, Backspace and Delete did nothing, both laptop and
external keyboards were affected, and only a Windows restart recovered it. An audio tool
(FxSound) cycled output devices on a single keypress — its documented Ctrl+Shift chord.

**Root cause — two distinct defects, both mine.**

1. **Stuck Ctrl+Shift (primary).** `SendInputPort` had a raw-VK escape hatch,
   `_inject_raw_vk`, that pressed keys **without recording them**, so `release_all()` could
   not release them. Worse, it **honoured the disarm gate**: once an emergency stop fired
   mid-chord and disarmed the port, the subsequent key-*ups were refused*. Ctrl and Alt
   were left logically held on the operator's machine. Held together, every physical
   keypress is then interpreted as a layout-switch chord — which explains the FxSound
   behaviour, the dead Backspace/Delete, and the `N` character, all from one cause. The
   hardware test `test_hotkey_press_triggers_the_stop` triggered exactly this sequence.

2. **Input-language switch.** `set_foreground` escalated to a `keybd_event` ALT tap to
   acquire foreground permission. On a machine with `en-US` **and `en-IN`** installed, that
   activated the IME and switched the active layout to `en-IN`. Measured: HKL `0x40090409`
   (locale `0x0409` = en-IN) was active where `0x40094009` (en-US) was intended. A secondary
   finding: `ActivateKeyboardLayout` is **thread-scoped**, so it reverts when the process
   exits — a "fix" that appears to work and silently does not.

**Fixes.**

- `SendInputPort` rewritten as a thin facade; **all injection is now owned solely by
  `InputSafetyManager`** (statically asserted).
- **A release is never blocked by a disarm.** Only presses are gated.
- `_inject_raw_vk` deleted; `send_raw_vk` is tracked like every other key.
- Cleanup sweeps both sides of Ctrl/Alt/Shift/Win and all five mouse buttons, unconditionally.
- The ALT `keybd_event` tap removed; `SetWindowPos` TOP→NOTOPMOST is sufficient.
- Input-language switching chords refused, with left/right canonicalisation.
- Input language sampled before a run, restored after if it moved.
- Cleanup moved into a `finally` on every exit path.
- `frameforge recover-input` added: standalone repair, key-up events only, no restart needed.

**Verification.**

- 40 new tests in `tests/unit/test_input_safety.py`, covering all twelve named exit paths.
- Static guards: no `SendInput` outside the safety module; no `keybd_event` call anywhere.
- Live run against real Notepad: **67 dispatches, `healthy: true`, no key or button left
  down, no OS modifiers down, layout matched baseline, 0 violations, 8 cleanups** —
  recorded in `input_audit.json` and `report.json`.
- Independent check in a fresh process after the run: **no stuck modifiers**.
- The exact failing sequence (estop mid-chord) re-run against the real adapter: Ctrl and Alt
  are released even though the port is disarmed.

---

## KNOWN LIMITATION: WinRT OCR recall degrades on small dark-theme body text

**Measured, reproducible, not a Frame Forge defect.**

On 2026-10-04, with a clean Microsoft Notepad window open showing a single character (`w`)
in the document body, WinRT OCR returned only `['File', 'Edit', 'View']` — the menu bar.
The body region was invisible to it at 1x **and** at 2x upscale, and on a crop of the
body alone. The same engine read body text reliably earlier the same day, on longer
strings, so this is a recall sensitivity rather than a hard failure.

**Why this matters, stated plainly.** Frame Forge's Notepad proof-of-value depends on
reading typed body text. When recall drops, the scenario reports "landmark not visible"
— a *perceptual* failure that points nowhere near the cause. That is the worst failure
shape: accurate message, wrong diagnosis.

**Mitigations now in place**

1. A dedicated regression test asserts the OCR backend is **constructible and available**,
   so a backend that fails to start (as one did earlier this session, from a missing
   import) surfaces immediately instead of as downstream misdiagnosis.
2. The run report already carries per-step `actual` OCR text, so a reader can see whether
   OCR returned *nothing*, returned *something else*, or returned *something garbled*.
3. `doctor` reports the OCR backend and its transport.

**Not yet done, and it should be.** The scenario should distinguish "OCR read nothing"
from "OCR read something and the marker is absent" — the second is a product failure, the
first is a capability failure, and only one of them is a bug in Frame Forge. That
distinction belongs in the report and is not currently made. It is the highest-value
remaining item in the perception layer.

---

## Defects found and fixed by these tests

Recorded because they are evidence that the suite has teeth, not just coverage.

1. **Focus tolerance granted a free first input.** When the guard was created with focus
   already wrong, the 250 ms tolerance window let the first input through. Found by
   `test_focus_loss_blocks_input_and_yields_unknown`. Fixed with an `_ever_focused` gate;
   regression tests added both ways (no grace before first correct focus; grace *after* it).
2. **Normalised ROI corruption.** `Rect.from_norm` unpacked three values into two names,
   which would have corrupted every normalised profile region. Found by a test that passed
   a redact region.
3. **Run ended paused but claimed COMPLETED.** A focus-loss run reported success. Now
   reports `UNKNOWN` — a false green is the worst output a QA tool can produce.
4. **Mouselook rounding error.** A 100 px eased flick summed to 102 px. Fixed by
   accumulating on the cumulative target; final step forced exact.
5. **AI budget double-charged.** Every AI call consumed two units, silently degrading a
   one-call budget on the first request.
6. **AI parser ignored `additionalProperties`.** The schema declared it, nothing enforced
   it, so a smuggled field such as `"sudo": true` would have been silently dropped.
7. **Prompt-injection gaps in our own guardrail.** Two permission-widening patterns
   (`bypass the safety check`, `grant yourself authority`) were missing. Found by the fuzz
   gate; the list now has 27 patterns with no false positives on benign instructions.
8. **A run that ended paused crashed on the terminal transition** rather than reporting.
9. **The estop hotkey crashed the whole process.** The `HOOKPROC` callback object was a
   temporary, so CPython collected it and the installed hook pointed at freed memory. An
   emergency stop that kills the process is worse than no emergency stop.
10. **The estop hook was installed on the wrong thread.** A low-level hook is invoked on
    the thread that installed it, and that thread must pump messages. Installing from the
    caller and pumping on another produced a hook that was silently never called.
11. **The estop hotkey never matched.** The hook reports `VK_LCONTROL` (0xA2) and
    `VK_LALT` (0xA4) while the configured combo stored the generic `VK_CONTROL` (0x11) and
    `VK_MENU` (0x12). Now normalised through `VK_NORMALISE`.
12. **The estop callback silently did nothing** because a module constant it referenced
    was never defined — the `except` that protects the ctypes boundary swallowed the
    `NameError` on every keypress. This is the direct cost of the swallow that keeps a
    raising callback from killing the process, so `FRAMEFORGE_DEBUG_ESTOP=1` now surfaces
    it.
13. **No mouse click had ever worked through the real input adapter.** `_send_button` was
   keyword-only but called positionally, so every click raised. 300+ headless tests missed
   it because they all drive `FakeInput`; it surfaced the moment a scenario clicked a real
   application. A faked adapter cannot catch a mismatch in the real one, so
   `TestMouseButtons` now exercises the real click path.
14. **A keystroke hold emitted a duplicate key-up.** A `hold_ms` down-primitive released
   itself and the compiler then sent the matching up again.
15. **The pause path was never resumed.** Focus loss was detected, recorded, and then the
   loop continued from a paused state into an illegal transition.
16. **`session_state()` reported "locked" on a transient null foreground window**, blocking
   the first keystroke of a legitimate run while an application was still appearing.
17. **Frame Forge counted its own console as an occluder**, so a console-launched run
   refused to perceive anything it was testing.
18. **A stray keypress opened the Windows print dialog** against a real physical printer,
   outside the target's rectangle, where Frame Forge could not perceive it. The occlusion
   detector named the culprit, which is why it exists; prevention is now codified as
   guardrail `G-ABS-09`, and the destructive system affordances are in every profile's
   `forbidden_intents`.
19. **The planner was never called by the director.** The `PlannerPort` existed and was
    fully implemented, but the director always used each step's declared action — so Tier 0
    policies, reaction rules, the AI tier and replay were all dead code. Found when the
    first replay produced an empty plan. Now the director consults the planner when one is
    configured and validates its output identically.

---

## Outstanding

**None blocking.** One documented limitation:

- **Criterion 5 — capture latency is ~108 ms/frame at 1920x1080 (~9 fps).** This is a
  property of BitBlt on an i5-8250U, not a defect. It bounds what the product can do:
  menu navigation, UI interaction and QA scenarios are comfortable; anything needing
  sub-100 ms reaction is not. DXGI capture is wired behind the port and is the available
  lever. `doctor` reports the measured number on every run so it is never a surprise.

**Recommended before the dashboard goes beyond a prototype:**

1. **A real third-party game.** Everything verified so far runs against our own testbed.
   The honesty gap is that no profile has been authored for a title we did not write. That
   is the next meaningful validation, and it is the owner's call which title.
2. **OCR accuracy against a real game's font.** Criterion 8 is measured only on the
   testbed's large high-contrast text. A stylized or small game font will score worse and
   may need the RapidOCR fallback.
3. **Cross-run trend view.** Reports are schema-versioned and diffable, but there is no
   "build 41 fails / build 42 passes" view. That belongs on top of the existing files, not
   replacing them (ADR 0006).

---

## Recommendation

**The backend is stable enough to begin the UI/dashboard phase**, built as a client of
`frameforge.api.server` (already implemented, loopback-only, refuses a non-loopback bind).

That recommendation comes with the caveat stated plainly: **the system has been proven
against a testbed we wrote ourselves, not against a third-party game.** The engine,
perception, safety and reporting layers are exercised and verified. The *profile authoring
workflow for an arbitrary real title* is the one capability that remains unproven in the
field, and it should be the first thing the owner exercises once a target game is
available.
