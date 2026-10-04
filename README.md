# Frame Forge

AI-powered universal autonomous game-playing and game-QA system for Windows.

Frame Forge operates **running games** through visible, user-level computer interaction:
it observes the screen, classifies the visible state, chooses a permitted action, executes
real keyboard/mouse input, verifies the result, collects evidence, and reports.

It is **not** a coding agent. It does not read game source, create patches, or build games.
See `docs/AI_GUARDRAILS.md` §2.

---

## The honest contract

What Frame Forge **is**, today:

- **A universal Windows UI operator.** It can drive any visible interface - launchers,
  menus, dialogs, settings - by looking at it. This is the capability that makes the
  product real, and it works with no AI provider configured.
- **A game-QA instrument.** It runs a scenario, evaluates assertions against captured
  evidence, and emits a report whose first section a developer reads to decide what to fix.
  Verified: it detects a deliberately planted defect and reports `PASS` when the defect is
  removed.
- **A profile-driven player.** Seven control-profile presets cover FPS, open-world, racing,
  RTS, city-builder/sim, sandbox/navigation and ordinary UI input models. Onboarding a
  title means authoring profiles, not writing code.
- **Deterministically reproducible.** Every run records the exact logical action sequence;
  `frameforge replay` re-runs it and diffs the result.

What Frame Forge **is not**, stated plainly:

- It cannot read state that is not visible on screen. Non-visual state requires a
  developer-provided, authorised test hook (declared per profile).
- It cannot capture fullscreen-*exclusive* games. That is a display-driver decision, not
  something we can code around. Borderless/windowed works natively.
- It is not currently suited to anything needing sub-100 ms reaction. Measured capture is
  ~108 ms per 1080p frame on this hardware.
- "Universal" means universal **at the core**, not zero-setup on any title. Authoring a
  profile is real work, and it is the main barrier to using this on a new game.
- It is verified against a real third-party application (Microsoft Notepad), which is
  evidence of universality in practice — but Notepad is a plain Win32 window. **Modern
  Windows applications that re-host as UWP/XAML islands** (Calculator, Control Panel,
  Settings) report `Windows.UI.Core.CoreWindow` and are a different problem: they are
  composited, not separate HWNDs, and Frame Forge's window model does not yet handle them.
  See `docs/STABILITY.md`.
- It has not yet been pointed at a *game*. Authoring a profile for an arbitrary title is
  the one capability still unproven in the field.

---

## Status

Backend and core are complete and verified. **No UI has been started** - that work begins
only after the stability gate in `docs/STABILITY.md` is signed off.

| | |
|---|---|
| Tests | **344** (319 headless in ~16 s, 25 hardware-in-the-loop) |
| Against our testbed | 5/5 PASS — real window, real capture, real WinRT OCR, real `SendInput` |
| **Against Microsoft Notepad** | **9/9 PASS** — launched, focused, typed, verified, cleared |
| QA canary | detects a planted defect -> `FAIL`; `PASS` when the defect is removed |
| Replay | a live run's plan replays exactly |
| Stability | 30/31 criteria met; 1 documented limitation (capture latency) |

### The universality claim, actually demonstrated

Everything was originally verified against a testbed we wrote ourselves, which is the
weakest possible evidence of universality. So Frame Forge now also runs against **software
it knows nothing about**:

```
profiles/games/notepad.json                  authored from a live OCR read of the window
tasks/scenarios/notepad_document_roundtrip.json
  -> launch Notepad, acquire foreground over a composited window, focus the editor,
     type a marker, verify it on screen, select all, delete, verify it is gone
  -> PASS 9/9
```

The profile was written by pointing Frame Forge at a running Notepad and reading what OCR
actually reported — `File`, `Edit`, `View`, `Untitled` at measured normalised
coordinates — rather than guessing. Nothing about Notepad appears anywhere in the engine.

Doing this found **seven real defects** that 300+ tests against our own testbed had
missed, including one where *no mouse click had ever worked through the real input
adapter*. See `docs/STABILITY.md`.

---

## Install

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -U pip
.venv/Scripts/python -m pip install -r requirements.lock
.venv/Scripts/python -m pip install -e ".[dev]"
```

Optional extras, each degrading capability rather than failing:

| Extra | Adds | Without it |
|---|---|---|
| `dxgi` | accelerated capture | BitBlt at ~108 ms/frame |
| `ocr` | in-process WinRT OCR | the persistent PowerShell host is used instead |
| `ocr_fallback` | RapidOCR for stylized fonts | vision-only mode; text assertions -> `UNKNOWN` |
| `api` | control API for the dashboard | CLI only |

---

## Use

```bash
frameforge doctor                 # honest capability report, with measured timings
frameforge validate               # check every profile and scenario parses
frameforge simulate --task tasks/scenarios/testbed_smoke.json \
                    --profile profiles/games/testbed.json
frameforge run --task tasks/scenarios/testbed_smoke.json \
               --profile profiles/games/testbed.json
frameforge inspect runs/ff-<...>  # summarise a past run
frameforge replay  runs/ff-<...>  # re-run a recorded plan and diff it
frameforge serve                  # local control API (dashboard backend)
```

`simulate` runs the **real** director, compiler, executor, verifier and report builder with
the Windows adapters replaced by fakes. It proves plumbing, not perception - and it prints
that caveat itself. Perception is proven by `run` against a real window.

### Trying it without a game

```bash
.venv/Scripts/python fixtures/testbed/testbed_app.py --title "FrameForge Testbed"
# in another shell:
frameforge run --task tasks/scenarios/testbed_smoke.json \
               --profile profiles/games/testbed.json
```

To see the QA canary work, seed the defect:

```bash
.venv/Scripts/python fixtures/testbed/testbed_app.py --bug healthbar --state gameplay --hp 20
frameforge run --task tasks/scenarios/testbed_healthbar_bug.json \
               --profile profiles/games/testbed.json
# -> FAIL, first divergence at the health bar step, with an evidence screenshot
```

---

## The one architectural commitment

The control loop is a **deterministic state machine**. Exactly one slot in it - "propose the
next logical action" - is pluggable, and AI fills that slot only if configured.

The AI emits *logical actions*, never input primitives. They pass through the same
validator, compiler, budget ledger, focus guard and postcondition machinery as scripted
steps. Consequences:

- The system is complete and useful with **no AI provider and no network**. `ai: off` is a
  first-class, tested mode, not a degraded one.
- An AI planner **cannot** bypass any guard, because there is no code path by which a
  planner emits input.
- QA runs are reproducible: every plan is recorded and replayable.
- `PASS` / `FAIL` / `UNKNOWN` are produced only by the deterministic verifier. The AI has
  no write path to any verdict.

---

## Authorised use

Intended for your own games and prototypes, developer-authorised QA builds, offline or
single-player games where automation is permitted, private sandboxes, and supervised
accessibility workflows.

**Not** for anti-cheat evasion, protection bypass, public competitive multiplayer
automation, account farming, matchmaking manipulation, or prohibited online play. These are
unconditional prohibitions in code, not guidelines - see `docs/AI_GUARDRAILS.md` §3. Every
profile must carry an owner's `authorized_use` attestation and is **rejected at load**
without one.

---

## Documentation

| Document | Contents |
|---|---|
| `docs/ROADMAP.md` | Full technical roadmap, phases, and the 31 stability criteria |
| `docs/AI_GUARDRAILS.md` | Numbered rules, each with an enforcement point and a test id |
| `docs/STABILITY.md` | The evidence pack: what is measured, what is not, defects found |
| `docs/adr/` | Nine architecture decision records with accepted costs |

---

## Repository layout

```
src/frameforge/
  kernel/       event spine, clock, ids, state machine, RunDirector
  ports/        every interface + fakes (no Windows code)
  adapters/     the only code that touches Windows APIs
  actions/      action model, compiler, executor, guards, budget
  perception/   anchors, health, assembler, verifier
  planning/     tier-0 policy, AI planner, validator
  tasks/        DSL, loader, runner, recovery ladder, replay
  qa/           reports
  store/        run directories and evidence
  cli/          doctor, run, simulate, inspect, replay, serve
profiles/       control presets, game profiles, launcher profiles
tasks/          scenarios
fixtures/       the testbed reference application
tests/          unit, integration, hardware
scripts/        probes used to measure this machine
```
