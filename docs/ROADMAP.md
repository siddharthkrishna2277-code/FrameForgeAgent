# Frame Forge — Backend/Core Technical Roadmap

Status: **proposal, awaiting owner approval.** No implementation has been written.
Author: lead architect / implementation agent
Date: 2026-10-03

> **Companion document: [`AI_GUARDRAILS.md`](./AI_GUARDRAILS.md)** — binding on all
> implementation from P0 onward. Guardrails are numbered, name their enforcement point
> in the architecture below, carry a test ID each, and have per-phase obligations
> (§16 of that document). Two of them are **release gates**: `G-INJ-04` (prompt-injection
> fuzz, ≥ 50 cases) and `G-VERD-01` (AI has no write path to a verdict). No rule may be
> marked enforced without a green test.

---

## 1. Executive summary

Frame Forge will be built as a **hexagonal (ports-and-adapters) Python 3.14 system** whose
nervous system is a **deterministic state machine**, not an LLM.

The single most important architectural decision: **the control loop is fixed,
deterministic and profile-driven; only one slot inside it — "propose the next
logical action" — is pluggable, and the AI fills that slot only if configured.** This
gives us three things at once: (a) the system is fully useful with **no AI provider
and no network**, (b) an AI planner can never bypass safety, action limits or
verification, because it does not emit input — it emits *logical actions* that go
through the same compiler, budget, focus-guard and postcondition machinery as
scripted steps, and (c) QA runs stay reproducible because a plan can be recorded and
replayed.

Everything below the loop is a **port** (interface). Windows specifics — DXGI capture,
BitBlt, `SendInput`, WinRT OCR, OpenCV — are **adapters** that can be swapped or
faked. This is what makes the backend testable with no display attached, and it is
also the seam that lets a future remote Executor run on a different machine
(§14) with zero redesign.

Development order is 9 phases, gated on **demonstrated capability, not on written
code**. The first proof of value is not "a game is being played" — it is:
**a self-contained testbed app is launched, driven through a real UI path, verified
by assertions, and a QA report is emitted that a developer can act on.** Game
profiles come after that spine is proven.

---

## 2. Honest feasibility and constraints

### What this laptop can do
- i5-8250U = 4 cores / 8 threads, UHD 620 (no CUDA, no Vulkan-class throughput), 12 GB RAM.
- Screen-region capture at 1920×1080 and OpenCV template matching are CPU-cheap
  (single-digit ms to low tens of ms). Perception at 2–5 Hz is comfortable.
- Multi-monitor extended capture is native and already verified present.
- ffmpeg is available for video encoding — no extra install needed.
- **Windows' built-in OCR engine works offline** with en-US/en-GB. This removes the
  single biggest dependency risk (Tesseract/EasyOCR) at zero install cost.

### What it cannot do — stated plainly, not buried
1. **No local LLM and no local VLM in the loop.** A 7B class model on 4c/8t + 12 GB
   yields ~2–4 tok/s and starves everything else; a vision-language model is simply
   not viable. Any "AI reasoning" must be **remote**, and the system must be
   complete without it. I am not going to pretend otherwise.
2. **Fullscreen-exclusive games are not capturable.** That is a display-driver
   design decision, not a bug we can code around. Frame Forge supports
   **borderless windowed / windowed** games natively and must *detect and report*
   the exclusive-fullscreen case as `capture_health: UNSUPPORTED` rather than
   silently producing black frames.
3. **"Universal" means universal at the core, not zero-setup on any title.**
   Fully autonomous competent play of an arbitrary unseen game, with no
   configuration, is not achievable and I will not claim it. What is achievable and
   is what we will build:
   - reliable, fully autonomous navigation of **any visible Windows UI** —
     launchers, menus, dialogs, settings, browsers (this is the general capability
     that makes the product real);
   - profile-driven play across a **broad class of input models** (FPS, open-world,
     racing, RTS, sim/builder, sandbox) via the logical-action vocabulary;
   - AI-assisted recovery and generalization **within** a configured objective,
     with honest `UNKNOWN` verdicts when perception is insufficient;
   - QA scenario execution with assertions, evidence and reports, in a game-agnostic way.
4. **One mouse, one focus target.** The user cannot browse or work in the same
   Windows session while a run is live. This is not a limitation to work around; it
   is a **safety requirement** the system must model (§12).
5. **The system sees only pixels.** It cannot know a hidden game state. Any assertion
   about internal state needs an *authorized* runtime signal (log file, exposed test
   hook, HTTP status endpoint) supplied by the developer as part of the scenario.
   This is the sanctioned seam between Frame Forge and the game under test.
6. **No anti-cheat, no online/multiplayer, no evasion.** Enforced as a hard startup
   policy (§15), not as a guideline.

### Performance envelope (design targets, to be measured in P2/P3)
| Stage | Target | Notes |
|---|---|---|
| Region capture (1920×1080, DXGI) | < 25 ms | adaptive: full-frame or ROI |
| Downscale for AI packet | < 15 ms | one-shot, cached |
| Template match set (≤ 20 anchors) | < 40 ms | grayscale + pyramid |
| OCR (WinRT, full frame) | 120–400 ms | async, cached per epoch |
| Decision cycle (Tier 0, no AI) | < 50 ms | polled, not busy |
| Decision cycle (Tier 1, remote AI) | 1.5–6 s | latency dominated, mitigated §5 |

---

## 3. Recommended technology stack

| Concern | Choice | Why this and not the alternative |
|---|---|---|
| Language | **Python 3.14, 64-bit** (already installed) | Only realistic option on this box with working Win32/WinRT bindings. Async + rapid iteration + the entire CV/automation ecosystem. Cost: GIL — accepted, we are I/O and native-call bound, not CPU-parallel bound. |
| Packaging | `pyproject.toml` + src layout + single `.venv` + `frameforge` console script | Reproducible; no PyInstaller until P7. |
| Capture (primary) | **DXGI Desktop Duplication** via `dxcam` | Hardware-accelerated path, low latency. Optional dependency. |
| Capture (fallback) | **`mss`** (BitBlt) | 1 MB, no DXGI edge cases, always works. **This is the default so P0–P3 are never blocked by a driver quirk.** Capture is behind a port, so promoting DXGI later is a config flag, not a redesign. |
| Vision | **OpenCV 5.0 (opencv-python, abi3 wheel)** | `matchTemplate`, `cvtColor`, HSV hist, SSIM/diff, contours, `putText` for annotation. Nothing heavier is justified. |
| OCR (primary) | **Windows.Media.Ocr (WinRT)** | Verified working offline on this machine. Zero install, zero model download, uses the OS's own tuned models. |
| OCR (fallback) | **RapidOCR + onnxruntime** (cp314 wheels confirmed) | Needed for stylized game fonts where WinRT underperforms. Onnxruntime is ~15 MB, still tiny, CPU-only. Optional extra. |
| OCR (excluded) | Tesseract | Not installed, needs a separate binary install + PATH plumbing, slower on CPU. Kept as a *pluggable third* backend only. |
| Input | **Raw Win32 `SendInput` via ctypes**, window mgmt via **pywin32** | Must be a single, ordered, injectable stream; mixing libraries is how input duplication bugs happen. `SendInput` is the same mechanism a keyboard driver uses, supports absolute+relative mouse, key down/up, all buttons, and XInput passthrough later. **PyAutoGUI is excluded deliberately**: sdist-only (no cp314 wheel), slower, no relative-mouse or hold semantics we need, and adds a failure mode. |
| Window / display / focus | **pywin32** (`win32gui`, `win32api`, `win32con`), `ctypes` user32 | Window identity, `SetForegroundWindow`, foreground polling, display mode query, DPI awareness. |
| Async runtime | **asyncio** (stdlib) | Single event loop owns the bus; the loop itself is sequential-by-design (see §5). |
| Schemas | **pydantic v2** (+ pydantic-settings) | Every action, observation, profile and report is a validated model. Validation *is* the safety layer for AI output. |
| Config | **YAML profiles validated by pydantic + JSON Schema export** | Human-authorable, diffable, versionable. |
| Storage | **Files, not a database**: `runs/<run_id>/` with `events.jsonl` + `report.json` + PNG frames + MP4 | Zero migration/dependency cost, trivially archivable, and a run directory *is* the evidence bundle a developer hands to a coder. SQLite only if P7 needs a query index. |
| Testing | **pytest** (+ pytest-asyncio) | Ports/adapters make the core 100% testable headless via fakes. |
| Logging | **structlog** → JSON lines, mirrored into the run's `events.jsonl` | One log stream = one audit trail. |
| Retry/backoff | **tenacity** (or ~30 lines of our own) | Only for capture/telemetry, never for input. |
| Video | **ffmpeg** (present) | Written to disk from captured frames; never piped through Python encoders. |
| AI (optional) | **Provider-agnostic adapter** over OpenAI-compatible chat/completions + a local-CLI adapter (Hermes `-z`, verified working on this machine) | Keeps providers replaceable; the CLI adapter means the AI tier is developable and testable today with zero API keys. |
| Control API (P7) | **FastAPI + uvicorn**, local-only bind | The future dashboard is a *client of this API*, so we define it in the backend phase and never write throwaway backend code for the UI. |
| Remote rig (future) | Same API + an `Executor` role swap (§14) | Zero redesign; the port that matters is the control transport. |

**Explicitly not used:** PyAutoGUI, Tesseract-as-primary, LangChain/LangGraph,
a vector DB, a message broker (Redis/Celery), Docker (no virtualization story on
this box, and it buys nothing for a single-session Windows desktop app), any local
ML model in the critical path.

---

## 4. Architecture

```
                            ┌──────────────────────────────────────────┐
                            │            RUN DIRECTOR                   │
                            │  deterministic state machine, single owner │
                            │  of: session → target → observe → decide   │
                            │        → act → verify → record → adapt    │
                            └───┬──────────────────────────────────────┘
                                │  typed events (pydantic), asyncio bus
   ┌────────────────────────────┼────────────────────────────────────────────┐
   │                            │                                            │
┌──▼───────────┐   ┌────────────▼─────────┐   ┌──────────────┐   ┌───────────▼────────┐
│  PERCEPTION  │   │      PLANNING        │   │   EXECUTION  │   │    ASSURANCE      │
│              │   │                      │   │              │   │                   │
│ CaptureSource│──▶│ Tier0 ProfilePlanner│──▶│ ActionCompil.│──▶│ FocusGuard         │
│ WindowIdent. │   │ Tier1 RemoteAIPlanner│   │ ActionExecutor│   │ UserInputDetector │
│ OcrEngine    │   │ Tier2 (stretch)      │   │ RateLimiter  │   │ Budget/Ledger     │
│ VisionMatch  │   │ PlanValidator        │   │ InputDriver  │   │ PostconditionEval │
│ DisplayHealth│   │ FallbackPolicy       │   │ Verifier     │   │ ConfidenceScorer  │
└──┬───────────┘   └──────────────────────┘   └───────┬──────┘   └─────────┬─────────┘
   │                                                  │                    │
   │                                            ┌─────▼──────┐             │
   └───────────────────────────────────────────▶│ VERIFY     │─────────────┘
                                                │ + RECOVER  │  (evidence → store)
                                                └─────┬──────┘
                                                      │
   ┌──────────────────────────────────────────────────▼───────────────────────────────────┐
   │                            PERSISTENCE / EVIDENCE                                   │
   │  EventLog(events.jsonl) · FrameStore(PNG, MP4) · RunStore(report.json) · ArtifactStore│
   └──────────────────────────────────────────────────┬───────────────────────────────────┘
                                                      │
                                        ┌─────────────▼─────────────┐
                                        │  CONTROL API (P7, local)   │
                                        │  run/stop/status/artifact │
                                        └────────────────────────────┘

        PORTS (interfaces)                    ADAPTERS (Windows implementations)
        CapturePort          ◄──────────────  DxgiCaptureAdapter / MssCaptureAdapter
        InputPort            ◄──────────────  SendInputAdapter (win32)
        OcrPort              ◄──────────────  WinRtOcrAdapter / RapidOcrAdapter / NullOcr
        VisionPort           ◄──────────────  OpenCvVisionAdapter
        WindowPort           ◄──────────────  PyWin32WindowAdapter
        PlannerPort          ◄──────────────  ProfilePlanner / RemoteAiPlanner / RecordedPlan
        ClockPort            ◄──────────────  SystemClock / FakeClock
        EventSink            ◄──────────────  JsonlEventSink / MemoryEventSink
        SafetyPort           ◄──────────────  FocusGuard / BudgetLedger / EstopSwitch
```

### Repository layout
```
frameforge/
  pyproject.toml  README.md  .gitignore  .python-version
  docs/            ROADMAP.md  ARCHITECTURE.md  STABILITY.md  adr/
  src/frameforge/
    kernel/        run.py director.py bus.py clock.py errors.py ids.py
    ports/         capture.py input.py ocr.py vision.py window.py planner.py sink.py
    adapters/
      capture/     mss_capture.py dxcam_capture.py
      input/       sendinput.py  fake.py
      ocr/         winrt_ocr.py rapidocr_ocr.py null_ocr.py
      vision/      opencv_vision.py
      window/      pywin32_window.py
      ai/          openai_compat.py cli_bridge.py recorded.py
    perception/    frame.py window_id.py anchors.py ocr_cache.py change.py health.py
    planning/      planner.py profile_planner.py ai_planner.py validator.py fallback.py
    actions/       model.py compiler.py executor.py verify.py ledger.py focus.py estop.py
    profiles/      schema.py loader.py control.py game.py launcher.py validate.py
    tasks/         dsl.py runner.py steps.py assertions.py
    qa/            scenario.py evidence.py report.py junit.py
    store/         runs.py artifacts.py video.py paths.py
    config/        settings.py profiles_dir.py
    cli/           main.py run.py inspect.py doctor.py simulate.py
  profiles/        control/*.yaml  games/*.yaml  launchers/*.yaml
  tasks/           scenarios/*.yaml
  tests/           unit/  integration/  hardware/  fixtures/
  fixtures/        testbed/   (our own small Win32/Pygame app: menu, loading,
                                 HUD counter, modal dialog, deterministic bug toggle)
  runs/            (gitignored artifacts)
```

### Component responsibilities (one line each, non-negotiable)
| Component | Responsibility |
|---|---|
| `RunDirector` | Owns the single sequential loop and all state transitions. Nobody else advances state. |
| `EventBus` | Typed pub/sub within the process. Every consequential thing becomes an event. |
| `CaptureSource` | Yields timestamped frames for a target (full desktop / monitor / window ROI). No interpretation. |
| `WindowIdentifier` | Maps a `TargetSpec` → live HWND, PID, class, title, monitor, DPI. Detects target loss. |
| `OcrEngine` | Frame → text lines with boxes + per-line confidence. Cached per frame epoch. |
| `VisionMatcher` | Frame + anchor set → anchor scores, color probes, structural diff. |
| `DisplayHealthMonitor` | Topology, resolution, DPI, mode, capture-liveness, black/frozen frame detection. |
| `FrameAssembler` | Merges capture + window identity + OCR + vision + health into one immutable `Observation`. |
| `ProfilePlanner` (Tier 0) | Deterministic policy: objective + observation + control profile → next logical action. |
| `AiPlanner` (Tier 1) | Optional remote reasoning producing a *structured plan proposal*, never input. |
| `PlanValidator` | Schema + safety + budget + action-permission check. Rejects anything malformed. |
| `FallbackPolicy` | On low confidence / repeated failure, degrade Tier1 → Tier0 → probe → halt. |
| `ActionCompiler` | Logical action + control profile + display metrics → concrete input primitive sequence. |
| `ActionExecutor` | Ordered, timed execution of primitives; hold/release lifetimes; interruption. |
| `RateLimiter` / `BudgetLedger` | Global and per-scope caps on actions and wall-clock; immovable limits. |
| `FocusGuard` | Continuous foreground/target check; gates every input; detects display changes. |
| `UserInputDetector` | `GetLastInputInfo`-based detection of human input → pause/hand back. |
| `EstopSwitch` | Global hotkey + API + file-watcher → hard disarm; input ports become no-ops. |
| `Verifier` | Evaluates a `Postcondition` → `PASS` / `FAIL` / `UNKNOWN` with evidence. |
| `ConfidenceScorer` | Aggregates perception + verification signals into a calibrated 0–1. |
| `TaskRunner` | Executes a task/scenario DSL: steps, retries, timeouts, branches, tags. |
| `EvidenceCollector` | Frames (key + delta), video, OCR snapshots, action history, timeline. |
| `ReportBuilder` | `report.json` + `report.md` + JUnit XML, structured for a developer. |
| `ControlApi` | Local HTTP: start/stop/status/artifacts. The future UI's only backend surface. |

---

## 5. AI design

### The division of labour
**AI reasons. It never acts, and it never defines truth.**

| Concern | Owner | Rationale |
|---|---|---|
| Screen capture, window identity, health | Deterministic | Must never hallucinate. |
| Anchor/OCR/colour matching | Deterministic | Reproducible, fast, cheap. |
| Menu/UI navigation (the dominant real use) | Deterministic + profile | Anchors + a step graph. This is 80% of practical value and must work with zero AI. |
| Objective → subgoal sequencing | Tier 0 default, **Tier 1 optional** | Tier 0 from the task definition; Tier 1 when the objective is open-ended. |
| Choosing among *legal* next actions under uncertainty | **Tier 1 (optional)** | Genuinely a reasoning problem — good AI use. |
| Action compilation, timing, safety, budget | Deterministic, non-bypassable | AI has no path to input except through here. |
| Outcome verification / pass-fail | Deterministic + profile assertions | A QA verdict must be reproducible and defensible. |
| Recovery strategy selection | Tier 1 proposes, deterministic gate decides | — |
| Narrative report writing | Tier 1 optional, **templated fallback always** | Reports must exist with no AI. |
| Learning / self-modification | **None.** No code writes, no profile self-edits. | Keeps the "no patches" boundary absolute. |

### Local baseline behaviour (Tier 0) — the system is complete without AI
1. **Anchor graph navigation.** A profile declares UI landmarks (template image + ROI + threshold, OCR regex + ROI, colour probe + ROI). The planner scores all landmarks each epoch and follows a declared transition graph: `main_menu → settings → graphics → back`.
2. **Reaction policies.** Ordered if/then rules over observations: `if loading_indicator_present → wait`, `if modal_dialog_present → dismiss(profile.dismiss_target)`, `if health_critical and potion_available → use_potion`.
3. **Task steps with pre/postconditions.** The scenario DSL covers the rest.
4. **Exploration fallback.** If a step's precondition is unmet and no reaction fires: a bounded, seeded search over declared candidate actions (`look_around`, `open_map`, `advance_waypoint`), with a hard cap. Deterministic given a seed.

This is a complete, useful, testable agent. It is not a stub standing in for a model.

### Remote AI (Tier 1) — optional, provider-agnostic
- **Perception packet** (small, structured, cacheable): objective + subgoal, a *downscaled* screenshot (optional, only for multimodal providers), top-N OCR lines, anchor scores, window state, last K transitions, allowed action list, remaining budget, and the profile's constraints. We never ship raw full-res frames unless asked.
- **Response contract:** strict JSON against a generated JSON Schema: `{"plan": [{"action": <logical action>, "rationale": str, "expected_change": <postcondition>}]}`, max N steps, plus `confidence`.
- **Enforcement chain (all mandatory):** schema validation → action-permission check (only actions the profile/objective permit) → compile to logical actions → budget/rate check → focus guard → executor → per-step postcondition. Any failure → `FallbackPolicy`.
- **Cost/latency control:** AI consulted only when Tier 0 is uncertain or stuck; decision cached per observation epoch; hard per-run AI-call ceiling; a run can be marked `ai: forbidden` and it still completes.
- **Recording & replay:** every AI plan is written to `plans.jsonl`. Re-running with `planner: recorded` reproduces the run exactly — this is how we get *deterministic regression testing of AI behaviour* and how we debug a bad run.
- **Provider adapters:** `OpenAiCompatPlanner` (any OpenAI-compatible endpoint, incl. local llama.cpp/Ollama later), `CliBridgePlanner` (Hermes `-z`, verified on this machine — used for development and for provider-free AI testing), `RecordedPlanner`. Config selects; no code change to add a provider.

### Tier 2 (stretch, only if time remains)
A tiny ONNX classifier (< 10 MB) trained on our own captured frames for `screen_class ∈ {loading, menu, gameplay, dialog, cinematic, crash}`. It accelerates Tier 0 decisions. It is **not** on the critical path; the anchor/heuristic path must stand alone.

### Confidence & calibrated honesty
Every `Observation` carries per-signal confidence. Aggregate score gates action:
| Score | Policy (`confidence_policy` in config) |
|---|---|
| ≥ 0.75 | act normally |
| 0.45–0.75 | Tier 0 restricted to low-risk actions; AI may be consulted |
| 0.20–0.45 | probe-only actions (`look_around`, `wait`, `screenshot`) |
| < 0.20 | halt, report `UNKNOWN`, save evidence |

A run's overall verdict is `PASS` / `FAIL` / `UNKNOWN` — **`UNKNOWN` is a first-class, expected outcome**, not an error. For a QA tool, "I could not determine this" reported honestly is more valuable than a confident wrong answer.

---

## 6. Action model and execution

One `Action` union, all pydantic-validated, all *logical* until compiled:

```
MoveMouse      {to: Point | to: Point, relative: bool, duration_ms}
Click          {button, at?, count=1, hold_ms?, modifiers?}
Press/Release  {button}                        # drag primitives
Drag           {path: [Point], button, duration_ms, steps?, easing}
Scroll         {at?, dx, dy, steps=1, duration_ms?}
KeyPress       {key}                            # tap
KeyDown/Up     {key}                            # explicit hold/release
Hotkey         {keys: [Key, ...], hold_ms=50}
TypeText       {text, method: unicode|scan, interval_ms}
MouseLook      {dx, dy, steps, duration_ms, curve: linear|ease}   # relative, for mouselook
GamepadButton  {button, down, hold_ms?}        # P5+, XInput, feature-flagged
GamepadAxis    {axis, value, duration_ms}      # P5+
GamepadStick   {stick, dx, dy, duration_ms}    # P5+
Wait           {ms}                             # explicit, rate-limited, never a blind sleep substitute
```

**Compilation.** `ActionCompiler` resolves the logical action through the active
`ControlProfile` (e.g. `sprint` → `LShift` on a WASD profile, `LeftTrigger` on an
XInput profile) and the active `DisplayProfile` (logical viewport coords → real
screen coords, DPI-aware, monitor-offset aware for the secondary display). Output is
a `Primitive` list: `MouseMoveAbs/MouseMoveRel/MouseButtonDown/Up/KeyDown/Up/Scroll/GamepadXInput/…`.

**Execution semantics.** The executor is a small serial state machine owning
*hold lifetimes*: a `KeyDown` without a matching `KeyUp` before a step boundary is
an error, and an emergency sweep releases everything on stop, exception, focus loss,
or timeout. Ordering is strict (input is a stream; concurrency here is a bug). A
`MouseLook` is emitted as N relative sub-moves with a sleep budget so it behaves
like a human flick and is interruptible mid-motion.

**Verification after every action** — never assume success:
- `screen_changed` (SSIM/mean-abs-diff over a declared ROI, with a noise floor),
- `region_changed(roi)`,
- `anchor_appeared/vanished(anchor)`,
- `text_matches(regex, roi)`,
- `window_state(expected)`,
- `signal_equals(key, value)` — *authorized* developer-provided runtime signal,
- `no_change_for(ms)` — a negative assertion, used for "loading should finish".

Each returns `PASS` / `FAIL` / `UNKNOWN`. `FAIL` triggers the step's retry policy
(backoff, cap) and feeds the confidence scorer. `UNKNOWN` propagates to the verdict.

---

## 7. Universal / profile design — no game hard-coded in the core

**Rule: the core contains zero game names, zero screen coordinates, and zero key
bindings. All of it lives in versioned YAML under `profiles/`, validated by pydantic
at load time with schema-version migration.**

| Profile | Contains | Example |
|---|---|---|
| `ControlProfile` | The **logical action vocabulary** and its bindings, per scheme, plus camera/look settings | `forward: WASD/w`, `sprint: LShift`, `interact: E`, `look: MouseDelta(sensitivity 0.35, invert off)`, `confirm: Enter/E`, `back: Escape` |
| `DisplayProfile` | Viewport ROI within the window, reference resolution, scale rules, DPI handling | reference 1920×1080, viewport = client rect |
| `GameProfile` | Identity (exe names, window class/title regex, process), required control profile, **landmarks**, **UI patterns**, **state hints**, **reaction rules**, optional **authorized signals**, action permissions, confidence policy | `identity.exe: ["MyGame.exe"]`, `landmarks: [{id: main_menu, ocr_regex: "MAIN MENU", roi: [0,0,1,0.25]}]`, `signals: [{name: hp, type: log, path: ..., pattern: "HP=(\\d+)"}]` |
| `LauncherProfile` | Not a special subsystem — a **task profile**: how to reach and start a title through a visible UI | Steam: focus window → OCR Library → click Play → wait for `window_title: "MyGame"` → verify readiness anchor |
| `TaskDefinition` | Objective, ordered steps (precondition → actions → postcondition), retries, timeouts, tags, evidence policy | — |
| `ScenarioDefinition` | A parameterised task + fixtures + environment + assertion set + expected outcomes + metadata (build id, commit) | — |

**Universality mechanism, stated honestly.** A new game is onboarded by *authoring
profiles*, not by writing code: pick a `ControlProfile` (six shipped presets cover
FPS, open-world, racing, RTS, sim/builder, sandbox/navigation), declare the game's
landmarks and UI patterns, declare the objective as a task, declare the QA
assertions. The engine is unchanged. The genuinely game-specific knowledge lives
in the profile, which is data — reviewable, diffable, and versionable. Optional
*authorized adapters* (a small plugin that reads an approved test hook) are allowed
for state that is not on screen, and are explicitly opt-in per profile.

**Ships-with-presets, not ships-with-guarantee.** The README will state: out of the
box Frame Forge can drive any Windows UI and can play a game once a profile exists
for it. That is the honest promise.

---

## 8. Launch modes — UI/Human Launch is primary

Both modes are implemented as the **same task pipeline** with different task
definitions. No special-casing, no fallback-to-subprocess shortcuts.

**Mode A — UI/Human Launch (default, primary).**
`focus Steam → verify state → navigate Library → find configured title → click Play →
handle visible launcher/dialog/loading states → detect target window → verify readiness
anchor → run task/scenario`. Fully implemented in P5 using only the perception/input
stack, with a seeded, bounded, evidence-captured fallback if Steam's layout differs
from the profile.

**Mode B — Direct/Test Launch (explicit, opt-in per profile/scenario).**
`subprocess` / `Start-Process` of an executable, a Steam URI, or a test-build CLI flag.
Marked `launch.direct: true`, requires an explicit `allow_direct_launch: true` in the
scenario plus a recorded justification. Rationale: legitimate and valuable for CI-like
repeatable QA, but it must be *visible in the report* so nobody mistakes a
direct-launched run for a human-path run. Report carries `launch_mode` and the exact
command line. A separate `verify_launch_path.py` check compares Mode A vs Mode B
outcomes so the fast path can't silently diverge from the human path.

**Mode C — Attach (no launch).** Attach to an already-running window. This is the
default for the first proof of value, because it removes launcher variables while
we prove perception and control.

---

## 9. QA, diagnostics, reporting

**Scenario = task + assertions + environment.** It is executed by the same runner
and produces the same evidence, then adds a verdict set.

**Assertion types** (all deterministic, all schema-validated):
- `anchor_visible` / `anchor_absent` — visual landmarks
- `text_present(regex, roi)` / `text_absent` — OCR-based
- `region_matches(reference_image, threshold, roi)` — golden-image comparison
- `color_probe(probe, tolerance)` — HUD/state colour checks
- `window_property(property, expected)` — title, class, size, monitor
- `signal_equals` / `signal_in_range` — **authorized** developer signals (log line,
  status file, local HTTP endpoint) — the sanctioned way to assert non-visual state
- `no_input_for(ms)` — "the game did nothing for 3 s" → fail
- `action_succeeded(action, postcondition)` — action-level postcondition
- `verdict_is(x)` — meta

**Evidence bundle per run** (`runs/2026-10-03_143012_ff-a1b2/`):
```
manifest.json        run id, seed, versions, profiles (with hashes), launch mode, argv
events.jsonl         every observation, decision, action, verification, health event
plan.jsonl           the exact logical action sequence, incl. AI plans (replayable)
timeline.jsonl       state transitions with timestamps + confidence
frames/              key frames, plus every FAIL/UNKNOWN, plus periodic anchors
video/run.mp4        ffmpeg-encoded, 15 fps sampled, only when evidence_policy enables
ocr/                 text snapshots at decision points
report.json          machine-readable verdict set + timings + health history
report.md            human/dev-readable narrative
junit.xml            optional CI integration
```

**Report structure — optimised for Herman/Cline.** The consumer is a coding agent
that will open the game project and decide what to fix. So the report leads with
*machine-actionable facts*, not prose:

1. **Verdict header** — `overall: PASS|FAIL|UNKNOWN`, counts by assertion, build id,
   profile versions, launch mode, whether AI was used, total wall time.
2. **Failure digest** — one entry per failed assertion: `assertion_id`, `scenario
   step`, `expected` vs `actual`, `confidence`, `first_divergence_step`, and the
   evidence pointers (frame path + timestamp + ROI). This is the section the coder
   reads first.
3. **Step trace** — compact table: step, actions (logical names), verification
   result, latency, confidence, screenshot link.
4. **First-divergence analysis** — the earliest point where observed state stopped
   matching the scenario expectation, with the frame and the state timeline. This is
   the single highest-value field for root-causing a gameplay bug.
5. **Health log** — capture drops, focus events, resolution changes, unknown states,
   AI fallbacks. Separates *"the game is broken"* from *"the harness lost the plot"*.
6. **Environment** — OS build, resolution/topology at start and end, versions, seed.
7. **Reproduction** — exact command to re-run, the recorded plan, and the profile
   hashes. Deterministic replay is the whole point.
8. **Narrative** — templated by default; AI-written only if configured, and clearly
   labelled as AI-generated commentary sitting *below* the facts.

`report.json` is schema-versioned and validated, so reports are diffable between
builds: *"build 41 → FAIL on `hud_health_visible`; build 42 → PASS"*. That trend
is what turns Frame Forge into a QA instrument rather than a demo.

---

## 10. State, confidence, failure, recovery, safety

**State model.** `IDLE → ARMING → ACQUIRING_TARGET → OBSERVING → DECIDING →
EXECUTING → VERIFYING → (RECOVERING | PAUSED) → ... → COMPLETED | FAILED | UNKNOWN |
ABORTED`. Transitions are explicit, logged, and the only writer is the `RunDirector`.

**Target identity & focus safety.**
- A `TargetSpec` (window class + title regex + pid + exe) is *re-resolved* every
  epoch, never cached blindly. A changed HWND for the same PID is a **new identity** →
  treated as target loss, not silently accepted.
- Before **every** input primitive batch, `FocusGuard` confirms foreground == target.
  Drift > `focus_tolerance_ms` (default 250 ms) → inputs are **not sent**; state
  becomes `PAUSED_FOCUS`, capture re-targets, and either (a) the run re-acquires and
  re-verifies the last postcondition before continuing, or (b) it aborts per config.
- Re-focusing uses `SetForegroundWindow` with the documented foreground-lock
  workaround, and **never** steals focus back from the user without an explicit
  policy — by default, a human click during a run means *hand back and pause*.
- `UserInputDetector` samples `GetLastInputInfo` every 250 ms. Any human input during
  a run → `PAUSED_USER` by default (configurable to `resume`), because a shared
  mouse makes concurrent human/agent action unsafe and non-reproducible.
- All input is suppressed unless the run is `ARMED` and the console/session is
  interactive (`WTSGetActiveConsoleSessionId` check — a disconnected session must
  never receive synthetic input).

**Emergency stop, three independent paths, all tested:**
1. Global hotkey (default `Ctrl+Alt+F12`) implemented via a low-level keyboard hook in
   a dedicated thread — must work even when the director is blocked on a slow AI call.
2. Local HTTP `POST /stop` and a watched sentinel file (`runs/<id>/ABORT`), so an
   external process or the future UI can stop it.
3. `SIGINT`/Ctrl-C on the console.
On any of them: disarm immediately, release all held keys/buttons, stop capture, flush
`events.jsonl`, write an `ABORTED` report with the action that was in flight. Disarm
is checked inside the executor, so even an in-flight action cannot complete.

**Timeouts & budgets.** Every step, phase, and run has a wall-clock timeout
(defaults: step 60 s, phase 300 s, run 3600 s — all config). Every run has hard caps:
`max_actions`, `max_actions_per_minute`, `max_ai_calls`, `max_unknown_states`,
`max_retries_per_step`. These are ledger-enforced, not advisory; the director checks
before each decision.

**Recovery ladder** (escalating, bounded, always terminating):
1. retry the action with jittered backoff (≤ 2)
2. re-verify perception; re-acquire window/focus
3. dismiss a known modal via profile rule
4. bounded deterministic probe (`look_around`, `open_map`, `advance_waypoint`) — seeded
5. ask Tier 1 AI for a recovery plan (if enabled and budget remains)
6. mark the step `UNKNOWN`, skip to next independent step
7. abort the run with a full evidence bundle
No infinite retries anywhere; each rung has a budget consumed from the ledger.

**Capture & display health.** Monitored continuously: frame liveness (timestamp
advance), black/frozen frame detection, resolution/DPI/mode change, monitor
add/remove, target occlusion/minimise, and exclusive-fullscreen detection (reported
as `UNSUPPORTED`, not silently black). Any change invalidates cached observations and
transforms, and forces re-acquisition.

---

## 11. Lightweight now, expandable later
- Core dependency floor: `pydantic`, `pywin32`, `mss`, `numpy`, `opencv-python`,
  `structlog`. `dxcam`, `rapidocr-onnxruntime`, `fastapi`/`uvicorn`, and all AI
  clients are **extras** with graceful absence (the system detects and reports
  "capability unavailable" rather than crashing).
- Every AI/OCR/capture implementation is chosen at runtime from a registry.
- `Tier2Classifier`, `GamepadInput`, `RemoteExecutor`, `MultiTargetOrchestrator`,
  and `WatchdogSupervisor` are declared extension points with stub ports from day one
  (a stub that raises `NotImplemented` with a clear message), so adding them later is
  additive.
- No plugin framework, no DI container, no code generation. Boring on purpose.

---

## 12. Future remote / dedicated executor
Design the seam now, build it later. Today `RunDirector` calls ports **in-process**.
The seam is a `ControlTransport` port with a `LocalInProcessTransport`
implementation. The P7 HTTP API is the first step toward a remote one. For a real
second machine: the Executor process runs the same adapters; a Controller (possibly
with the heavy AI) talks to it over the same API; the local loop in a remote run is
replaced by a `RemoteSignalAdapter` that awaits acks. Because *all* Windows specifics
are already behind ports, the remote path adds a transport and an ack model, nothing
else. Explicitly out of scope now — no assumption of a second machine anywhere in the
current design.

---

## 13. Risk register

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| R1 | Anti-cheat / ToS violation — product used for online competitive play or evasion | **Critical** | Hard startup policy check (see below). No protection-bypass code, ever, not even behind a flag. Online/MP titles are refused unless a profile is explicitly marked `offline_authorized` by the owner. |
| R2 | Fullscreen-exclusive capture is impossible | High | Require borderless/windowed; detect and report `UNSUPPORTED`; document early. |
| R3 | Shared mouse → agent fights the human, or user is injured by stray input | High | `UserInputDetector` pause-by-default, `FocusGuard` gate on every input, hard estop on 3 paths, hold-release sweep, and a mandatory 5 s "take control" grace before input is ever sent. |
| R4 | Perception false-confidence → wrong action, corrupted QA verdict | High | Confidence gating, `UNKNOWN` as a first-class verdict, mandatory postcondition on every action, no action on low confidence except probes. |
| R5 | "Universal" overclaim → user expects zero-config play on any title | High | README and CLI both state the honest contract; a `doctor` command reports what is supported right now. |
| R6 | Python 3.14 ecosystem churn (newest interpreter; some wheels may lag) | Medium | Wheels verified present for the chosen set; pin versions in a lockfile; keep a fallback interpreter path documented. Avoid sdist-only packages (this is why PyAutoGUI is out). |
| R7 | WinRT OCR interop is finicky from Python (async WinRT, apartment model) | Medium | Isolated in one adapter with a fallback to RapidOCR and a null OCR mode; adapter is unit-tested with recorded frames. |
| R8 | i5-8250U thermal/throttle + 12 GB RAM pressure with video encoding | Medium | Encode via external ffmpeg (already installed), sample video at 15 fps, cap in-memory frame ring buffer, no full-res frame accumulation. |
| R9 | Evidence bloat (thousands of PNGs) fills disk | Medium | Retention policy per scenario, delta-only frames, MP4 for sequences, per-run size cap with pruning of the oldest non-key frames. |
| R10 | AI provider cost / latency / nondeterminism ruins QA reproducibility | Medium | AI optional, call-capped, every plan recorded, `recorded` planner for exact replay, deterministic Tier 0 as the default path. |
| R11 | Game updates break profiles silently | Medium | Anchor scores and match rates reported per run; a profile-health check flags degraded anchors; profile schema versioning. |
| R12 | Accidentally running a real-money/online title | **Critical** | `authorized_use` block required in every profile; startup policy validates it; process-name denylist for known anti-cheat/online titles as a belt-and-braces refusal. |
| R13 | Maintenance: too many abstractions too early | Medium | Deliberately no plugin framework/DI container; ports only where a real second implementation exists or is imminent. |
| R14 | Secrets in logs (API keys, user data in OCR text) | Medium | Redaction filter on the event sink; `.env` never logged; OCR text in reports is opt-in per scenario. |

---

## 14. Build sequence, validation criteria, first proof of value

Nine phases. Each ends with a **runnable demonstration**, not a code-complete feeling.
I am deliberately sequencing so that the riskiest unknowns (Windows capture, SendInput
fidelity, focus safety) are proven in the first third, before any AI or profile work.

| Phase | Scope | Exit criteria (all must be demonstrated) |
|---|---|---|
| **P0** Foundation | Repo, venv, `pyproject`, lint, CI-less `pytest`, ports + dataclasses, **the testbed fixture app** | `pytest` green; testbed app launches and shows menu/loading/HUD/dialog; ports importable; `doctor` reports environment |
| **P1** Kernel | EventBus, Clock, RunDirector skeleton, Run model, ArtifactStore, EventLog, CLI (`run`/`simulate`), config | A simulated run produces a valid `runs/<id>/` with `events.jsonl` + `report.json`; full event stream asserted in tests |
| **P2** Perception | Capture (mss → dxcam), WindowIdentifier, DisplayHealth, OcrEngine (WinRT → RapidOCR), VisionMatcher, FrameAssembler, caching | Live capture of **both** monitors; OCR of a real window returns text+boxes; change detection < 50 ms on a synthetic ROI; health monitor detects a resolution change and a black frame in a test |
| **P3** Input & safety | ActionCompiler, SendInput adapter, Executor with hold lifetimes, RateLimiter, BudgetLedger, FocusGuard, UserInputDetector, EstopSwitch | Hardware-in-the-loop suite passes: move, click, dblclick, right-click, drag, scroll, key tap, hotkey, hold/release, text typing, relative mouse-look. **Estop releases all held keys within 100 ms.** FocusGuard provably blocks input on focus drift. Budget caps provably enforced. |
| **P4** Deterministic agent | Task DSL, steps, pre/postconditions, Verifier, ConfidenceScorer, Recovery ladder, Tier0 ProfilePlanner, ReportBuilder | End-to-end scenario in the **testbed app**: navigate menu → start game → observe HUD → trigger the seeded bug → assertion FAILs correctly → recovery ladder runs → report emitted. **This is the first proof of value.** |
| **P5** Profiles & launch | Profile schema/loader/migration, 6 ControlProfile presets, GameProfile authoring, landmark tooling (`annotate` CLI), LauncherProfile, Mode A/B/C | A real launcher path is driven end-to-end (Steam Library → Play → game window) **or**, if the target title is unavailable, a non-Steam launcher/browser path is driven to the same standard. Direct-launch mode verified equivalent. |
| **P6** AI planner | Provider adapters, perception packet, PlanValidator, budget/confidence integration, fallback, recording/replay, CLI-bridge provider | Same scenario passes with `ai: off`; with `ai: on` a scripted perception failure is recovered by the AI planner; a malformed/hostile AI response is **rejected** by the validator (fuzz-tested, ≥ 50 cases — `G-INJ-04` release gate); recorded replay reproduces the run. |
| **P7** QA hardening & API | Full assertion set, JUnit export, retention, ControlApi (FastAPI), supervisor/watchdog, packaging | API starts/stops/statuses a run headlessly; report reviewed by the owner and judged actionable; install-from-clean-venv verified. |
| **P8** | *(post-approval)* UI / dashboard | — |

### First proof of value (P4 deliverable, stated concretely)
Frame Forge launches the **testbed app** (our own ~600-line fixture: main menu with
three buttons, an animated loading screen, a gameplay view with a live HUD counter
and a minimap, a modal error dialog, and a **seeded deterministic bug** — e.g. "the
health bar fails to render when HP < 30", toggleable by a CLI flag), navigates it
purely through visible UI, performs a scripted gameplay interaction, verifies the
outcome by assertions, catches the seeded bug, produces a report whose
**first-divergence analysis** points at the right step, and generates a 30 s video.
Then it is pointed at one real game the owner nominates, with a profile authored in
P5, under supervision.

**Why a self-built testbed first:** it makes the whole system deterministically
testable and regression-safe, gives us ground-truth bugs on demand, keeps the first
milestone independent of game availability, and doubles as the fixture for every
later phase. It is the correct engineering choice, not a dodge.

---

## 15. Backend-stability criteria (gate before any UI work)

UI/dashboard work does not begin until **every** item below is evidenced in writing
(`docs/STABILITY.md`) and demonstrated live. No exceptions for "it's basically done".

**A. Correctness & control**
1. 100% of core logic covered by automated tests; `pytest` green; no test requires a
   physical display (ports + fakes).
2. Hardware-in-the-loop input suite passes on this machine, including hold/release
   leak checks and mid-action estop (< 100 ms to zero held inputs).
3. FocusGuard demonstrated to block input on focus drift in a scripted drill.
4. Zero input is ever sent while the run is not `ARMED` (tested by inspection + test).

**B. Perception**
5. Sustained capture at the design target on both monitors, with measured latencies
   in the §2 table (or a documented, accepted deviation).
6. Display/topology change during a run is detected and handled without a crash and
   with a correct report entry.
7. Exclusive-fullscreen is detected and reported `UNSUPPORTED`, not silently black.
8. OCR adapter passes a recorded-frame accuracy check (≥ 95% on clear UI text in our
   own fixture; document game-font limitations honestly).
9. Guardrails: `G-PER-01..06` enforced — TEST-PER-02 (redaction), TEST-PER-03 (no
   secrets in logs/reports), TEST-PER-05 (zero sockets opened during an `ai: off` run).

**C. Determinism & AI containment**
10. A complete run with `ai: off` succeeds end-to-end. This is the single most
    important criterion.
11. A recorded run replays identically (same logical action sequence) — verified by
    diffing `plan.jsonl`.
12. Malformed, out-of-schema, over-budget, and permission-violating AI plans are all
    rejected by the validator (fuzz-tested, ≥ 50 hostile cases).
13. No code path exists by which a planner emits an input primitive directly.
14. **RELEASE GATE — `G-VERD-01`:** static check proves no AI-adapter module is in the
    verdict computation's import graph; TEST-VERD-01 green.
15. **RELEASE GATE — `G-INJ-04`:** prompt-injection fuzz over OCR text, window titles,
    button labels and AI responses — ≥ 50 adversarial cases, all rejected or downgraded.

**D. QA & reporting**
16. A scenario with a deliberately seeded bug produces `FAIL` with correct
    first-divergence analysis; a clean run produces `PASS`; an ambiguous case
    produces `UNKNOWN` (all three demonstrated).
17. `report.json` is schema-validated; `report.md` is reviewed by the owner and judged
    actionable by a coding agent without further questioning.
18. Every run directory is self-contained and archivable; retention works.

**E. Safety, policy, operations**
19. Estop verified via all three paths; a full abort produces a complete report.
20. Budget/rate caps demonstrably stop a runaway run.
21. `authorized_use` policy enforced at startup; the online/anti-cheat refusal path
    is tested.
22. `doctor` gives an honest capability report (what's installed, what's supported,
    what's degraded).
23. Secrets never appear in logs or reports (redaction test).

**F. Maintainability**
24. Clean install from a fresh venv using the lockfile, verified on this machine.
25. Public API is documented; every port has at least one real adapter and one fake.
26. No game names, coordinates, or key bindings anywhere in `src/frameforge/kernel`,
    `actions`, `perception`, or `planning` — enforced by an automated grep test.
27. ADR log explains the load-bearing decisions (capture default, OCR choice, input
    layer, Tier0-first, files-not-DB).

**Owner sign-off is required.** I will present the evidence pack, not just assert it.

---

## 16. Decisions I need from the project owner

Blocking (I need an answer to start):
1. **First real target game** for P5/POV — which title, and is it borderless-windowed-capable?
2. **Authorisation posture** — confirm the intended-use list is the user's own
   prototypes + offline titles only, and that no online/multiplayer/anti-cheat title
   will ever be targeted. (Design enforces this; I want it on record.)
3. **AI provider** — remote API key(s) available, or should the default be the
   local Hermes CLI bridge (no keys, slower)? Or default `ai: off`?
4. **Testbed app** — OK for me to build a small fixture game as the first target?
   (I strongly recommend yes; it is the backbone of the test strategy.)

Non-blocking (I'll proceed with my stated default and you can change later):
5. Python 3.14 confirmed, or pin 3.12 for a wider wheel margin? *(default: 3.14, wheels verified)*
6. Direct-launch mode enabled? *(default: opt-in per profile, recorded in reports)*
7. Report destination: local Markdown/JSON only, or also push to an issue tracker? *(default: local only)*
8. Secret handling: `.env` + OS keyring, or env vars only? *(default: env vars, `.env` gitignored)*
