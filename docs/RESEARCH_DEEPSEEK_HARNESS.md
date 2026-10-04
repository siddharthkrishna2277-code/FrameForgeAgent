# Research: DeepSeek Harness (`dsh`) — architecture audit

**Mode: research only. No harness changes were made. No dsh package installed, enabled,
or vendored.**

Primary sources read directly from `github.com/deepseek-ai/deepseek-harness` @ `master`:
`README.md`, `SAFETY.md`, `packages/AGENTS.md`-level READMEs for `computer-use`, `guard`,
`sandbox`, `shell`; `docs/capability-seams.md`, `docs/tool-execution-pipeline.md`,
`docs/defensive-patterns.md`, `docs/subsystems/computer-use.md`; plus the GitHub contents
API for the repository, `apps/`, `packages/`, `docs/` and `docs/subsystems/` trees.

I did not rely on videos or third-party write-ups.

---

## 1. Executive summary

`dsh` is a real, substantial, MIT-licensed agent harness: TypeScript/Node, built on
**Cordis**, created 2026-08-13, last pushed 2026-10-03, with **54 packages** and a
**119-file subsystem documentation set**. It is serious engineering — `SAFETY.md`,
`THIRD_PARTY_NOTICES.md`, generated doc graphs with `gen-doc-graphs` staleness markers,
`.agents/notes/` decision records, per-platform sandbox backends.

**The finding that decides the recommendation:** dsh already contains a
**computer-use capability** — `ctx.computerUse`, an *exclusive provider registration*
serving "Cua Driver" desktop control, with MCP and native providers. It is experimental
and requires explicit activation, but it exists.

That makes this **not** a decision about whether to adopt a harness for input control —
dsh already tries to do that, and its own safety notice disclaims it. It is a decision
about whether Frame Forge should let *anyone else's* agent hold a desktop-input
registration, when Frame Forge has already suffered two release-blocking incidents from
exactly that.

**Recommendation: Option A/D hybrid — borrow the architecture, integrate nothing.**
Specifically: adopt four *patterns* (below), build Frame Forge's own equivalents, and do
**not** depend on dsh as a runtime component in any tier.

**Recommendation strength:** high, and it is not close. The primary reason is not
immaturity — dsh is candidly developer-preview, and honesty about that is to its credit —
it is that **dsh's threat model is the opposite of Frame Forge's.** dsh's sandbox is
explicitly "same-world only" and its computer-use provider states plainly that *"[a]
cancelled call cannot undo input that the desktop already received."* Those are reasonable
statements for a general coding agent. They are disqualifying for an agent that moves a
single operator's physical mouse.

---

## 2. Repository map

Root: `apps/`, `packages/`, `native/`, `python/`, `docs/`, `patches/`, `vendor/`, `website/`,
`benchmarks/`, `snapshots/`, `scripts/`. Tooling: **pnpm** workspace + TypeScript, with
vitest (7 config files: e2e, web, snapshot, bench, shared, expected, web-stress, web.perf),
Bazel-adjacent `patches/`, `lefthook.yml`, `Makefile`, `oxlint`, `jscpd` (copy-paste
detection), and a Python side with `pytest.ini` — so it is polyglot, not pure TS.

`apps/` (4): `cli`, `web`, `desktop`, `desktop-host`.

`packages/` (54), grouped by intent:

| Group | Packages | Note |
|---|---|---|
| Core spine | `core`, `context`, `boot`, `host`, `cordis`-facing `bundle`, `preset`, `settings`, `extensions`, `features` | plugin tree, services, events |
| Agent | `agent-loop`, `agent`, `subagent`, `plan`, `goal`, `todo`, `workflow`, `skill` | |
| Session/state | `session`, `session-query`, `compaction`, `history`, `state`, `thread-store`, `spill`, `storage` | |
| Model | `llm`, `client`, `api`, `realtime-*`, `token-meter`, `model-provider` | |
| **Tools/safety** | `guard`, `sandbox*`, `shell`, `fs`, `terminal`, `subprocess`, `jobs`, `credentials`, `hooks`, `secrets` | **most relevant to Frame Forge** |
| Desktop | `computer-use`, `browser-use`, `interaction` | |
| Integration | `mcp`, `lsp`, `webhook`, `ssh`, `otel`, `analytics`, `feedback` | |
| Surface | `web`, `acp`, `sdk`, `ptc-runtime`, `client` | |

---

## 3. Core architecture

```
                 ┌──────────────────────────────────────────┐
                 │  Cordis shared context  (ctx)            │
                 │  every plugin: apply() → effects         │
                 └──────────────────────────────────────────┘
                                   │  registers / disposes
     ┌────────────────┬──────────────┼───────────────┬──────────────────┐
     ▼                ▼              ▼               ▼                  ▼
  services        typed events   reversible      plugin tree        profiles
  ctx.shell      hooks fire     effects          (load/unload)    → bundles
  ctx.tools      and return     (disposers)        │              → patches
  ctx.sandbox                    │                  │                  │
  ctx.computerUse                ▼                  ▼                  ▼
                              out-of-tree      hot reload        layered config
                                plugins        (ctx.hmr)        ownership
```

Three ideas carry the design:

1. **Everything is a plugin.** A plugin's `apply(ctx)` registers services and listeners and
   returns *disposers*. Unloading calls the disposers, so registrations reverse.
2. **Exclusive service registration.** `ctx.computerUse.register(name)` "reserves the sole
   provider slot until the contribution is disposed. A second registration fails even when
   it repeats the current name." Mounting two `ctx.shell` executors "fails loudly at load
   time."
3. **Layered composition.** Profiles select ordered bundles; later patch layers override
   configuration. One engine, several launch modes (web / headless / sdk / sdk-minimal / acp).

---

## 4. The tool-execution pipeline — the most valuable artifact

This is the single most transferable thing in the repository, and it is *generated* into a
mermaid diagram so it cannot silently drift from the code.

```
model tool-call block
  → session event tool/call          (logged BEFORE execution)
  → tools/pre-execute waterfall      (hooks, permission, sandbox)
      ├─ deny  → body skipped, recorded as a fact
      └─ ask   → ctx.approval one-shot prompt
                 ├─ allowed-once → continue
                 └─ rejected / cancelled / unavailable → DENY
  → registered monotonic guards      (deny or abstain; identity protected)
  → tools/execute waterfall          (timeout, retry, metrics — around dispatch)
  → tool execute() body
  → fs/write-intent gate             (tool-fs mutations only)
  → tools/post-execute waterfall     (accept, block, replace, add context)
  → finalizeContent                  (last content-only invariant)
  → tools/result                     (frozen authoritative outcome)
  → session event tool/result
```

Details worth stealing individually:

* **"Absent or unanswerable: deny."** An approval prompt that cannot be answered fails
  **closed**. Frame Forge has no equivalent and should.
* **Monotonic guards cannot be reordered.** "owner policy that must not be reordered
  remains a registered guard" — the waterfall may transform a call, but policy registered
  as a guard is final. This is the ordering property Frame Forge's policy layer lacks.
* **Frozen authoritative outcome.** `tools/result` is immutable and lossless; a snapshot
  failure is normalised to `isError` rather than escaping.
* **Tool-call logged before execution.** Frame Forge logs actions *after* sending them.

---

## 5. `defensive-patterns.md` — bugs they actually shipped

This document is the highest-signal file in the repo. It is explicitly "bug-class rules:
each pattern below is a class of defect that actually shipped or nearly shipped here."

Four map onto Frame Forge's two incidents with uncomfortable precision:

| Their rule | Frame Forge's experience |
|---|---|
| **"Dispose must reach quiescence, not just request it"** — "a teardown that issues kills/aborts but returns before the work stops leaves orphans… await the children's exit; close listener registries BEFORE killing so late completions stay silent" | Incident 1: the estop fired, disarm blocked the key-ups, and the release never completed. |
| **"Report orthogonal outcomes independently"** — "never nest one flag's report inside another's branch, or a caller reads a cut-short run as a clean success" | `ExecutionResult.primitives_sent` was `0` on a partial send, indistinguishable from a clean zero-action run. |
| **"Never hand untrusted output the ambient environment"** — spawned commands get a scrubbed env (drop `*KEY*`/`*SECRET*`/`*TOKEN*`) | I added exactly this two turns ago, independently, from `process-hardening`. Convergence on the same rule from two directions is reassuring. |
| **"Unlink link-shaped paths"** — `lstat` then `unlink`, never `rm -rf` through a junction; Windows `rmSync` throws `EISDIR` on a junction | Frame Forge's `EvidenceStore._safe()` resolves and prefix-checks paths but does not lstat. |

Also relevant: "**Async state is not synchronous state**" — `whenIdle()`/`status` are not the
result of one follow-up, and "if the awaited transition can never occur, the wait hangs, so
handle the 'nothing to wait for' branch explicitly."

---

## 6. `computer-use` — where dsh and Frame Forge collide

Verbatim from `docs/subsystems/computer-use.md`:

* The service "registers only a name and rejects any second provider… It has no common
  desktop-operation methods **or model-controlled selector**."
* "**One registered provider does not reserve a desktop for a Session.** Callers coordinate
  complete observe, act, and verify workflows across Sessions and separate DSH processes."
* "**A cancelled call cannot undo input that the desktop already received.**"

That last sentence is the crux. It is an accurate statement of the physical reality Frame
Forge spent two release-blocking incidents learning — and dsh accepts it as a limitation of
a *general-purpose* coding agent whose computer-use is off by default and experimental.

Frame Forge's position is the opposite: input safety is the product. A harness whose stated
position is "cancellation cannot undo delivered input" cannot be the component holding the
mouse in a system whose entire incident history is delivered input that could not be undone.

---

## 7. Current-vs-DeepSeek gap table

| Area | Frame Forge today | dsh pattern | Keep | Adapt | Build | Avoid |
|---|---|---|---|---|---|---|
| Core/orchestrator | `RunDirector` owns the loop, single-threaded | Cordis `ctx`, plugins contribute services | ✓ | — | — | adopting a second orchestrator |
| Module registration | Python imports; ports + adapters | `apply(ctx)` → services/listeners/disposers | ✓ | **reversible registration** | — | — |
| Reversible effects | `finally` blocks, `release_all()` | disposer-returning `apply()` | ✓ | **disposer registry** | — | — |
| Exclusive registration | none | second registration **fails loudly** | — | **adopt** | — | — |
| Monotonic policy order | policy runs before dispatch, order untested | guards cannot be reordered | ✓ | **adopt explicitly** | — | — |
| Approval | none (policy allows/denies) | `ctx.approval`, **absent ⇒ deny** | — | **adopt fail-closed approval** | — | optional approvals |
| Tool registry | `Action` union + `ToolRequest` | scoped `ctx.tools` registry | ✓ | scope per workspace | — | — |
| Guarded execution | `InputPolicy` → `InputController` | 3 waterfalls + guards | ✓ | — | — | — |
| Audit log | `events.jsonl`, append-only, redacted | `tool/call` logged **before** execution | ✓ | **log before send** | — | — |
| Frozen outcome | `ToolResult` (mutable dataclass) | `tools/result` immutable + lossless | ✓ | **freeze** | — | — |
| Session log | per-run dir | durable cross-session store | — | — | later | premature |
| Profiles/bundles/patches | YAML profiles | profile → bundle → patch | ✓ | — | — | full layering |
| Hot reload | none | `ctx.hmr` | — | — | — | **now** — bad with live input |
| Sandbox | none (process hardening only) | Seatbelt / bwrap+Landlock / Windows ACL | ✓ | — | **maybe** | — |
| Shell | none (spawns fixed binaries) | `ctx.shell` + sandboxed variants | ✓ | — | maybe | exposing shell to agents |
| Loop hygiene | budget ledger | `guard/repeat-tool-reminder`, `timeout-policy` | ✓ | **adopt both** | — | — |
| Plugins | Python modules | npm `dsh-plugin` | — | — | — | third-party plugins |
| UI | `api/server.py` (local, loopback) | web / desktop / sdk / acp | ✓ | — | — | — |
| IPC | HTTP | JSON-RPC, websocket, webrtc | — | — | — | now |
| Input safety | `InputSafetyManager`, deny-list, identity | *no equivalent* | **✓ ours is stronger** | — | — | importing theirs |
| Tests | 451 headless + 26 hardware | vitest + pytest + jscpd + benchmarks | ✓ | — | — | — |

---

## 8. Risk register

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| R1 | **A dsh agent holds a desktop-input registration** | **Critical** | Do not integrate. Frame Forge keeps sole ownership; no plugin may register input. |
| R2 | Developer-preview; "THERE WILL BE COMPATIBILITY-BREAKING CHANGES" | High | Nothing adopted at runtime; patterns only. |
| R3 | Their own safety notice: "has not undergone a security audit… must not be treated as secure" | High | Independent reason not to depend on it. |
| R4 | Polyglot TS+Python, pnpm workspace, 54 packages | Med | Vendor cost is high; pattern cost is near zero. |
| R5 | Third-party plugin supply chain | High | We already ship an in-process Input Safety boundary; no third-party code inside it. |
| R6 | 243k stars / 29k forks in ~7 weeks is an unusual ratio | Med | Treat popularity as weak evidence; I relied on source, not metrics. |
| R7 | Same-world sandbox shares kernel + filesystem | High | Already understood; ours is a *worse* problem (no fork boundary) and we do not claim otherwise. |
| R8 | Hot reload conflicts with live input ownership | High | Do not adopt `ctx.hmr` while input is live. |
| R9 | Scope creep toward a general agent platform | Med | Option A/D keeps the product narrow. |

---

## 9. Recommended option

**Option A + D: borrow the architecture; integrate nothing.**

*Why not C (adapter/plugin integration):* an adapter would make a preview, unaudited,
polyglot dependency sit in Frame Forge's process graph, and its computer-use provider
explicitly declines to own cancellation semantics. That is precisely the guarantee Frame
Forge exists to provide.

*Why not B (external tool):* dsh brings shell + filesystem + browser + terminal. Frame
Forge needs none of that; it needs a UI.

*Why not E (fork):* nothing to fork *for* — every pattern we want is a few dozen lines.

### Patterns to adopt now (all small, all testable)

| Pattern | Effort | Value here |
|---|---|---|
| **Absent approval ⇒ deny** (fail-closed) | small | closes a real hole: Frame Forge has no approval concept at all |
| **Monotonic guards: policy order is final** | small | makes "policy runs first" a tested property, not a convention |
| **Log `tool/call` before execution** | small | Frame Forge logs actions *after*; a crash loses the intent |
| **`repeat-tool-reminder`** | small | Frame Forge has retries and budgets; it has no stuck-loop detector |
| **`timeout-policy` as a first-class per-call deadline** | small | currently per-step only |
| **Orthogonal outcome reporting** | small | fixes `primitives_sent == 0` ambiguity |
| **Exclusive registration that fails loudly** | medium | one input controller, provably |
| **Disposer-based teardown** | medium | replaces scattered `finally` with a reverse-ordered registry |

### Explicitly NOT to adopt

1. **Any desktop-input provider.** Frame Forge's `InputSafetyManager` stays the only
   emitter. No dsh plugin, agent, model adapter, UI or generic tool may reach it.
2. **The plugin/npm distribution model.** Third-party code in the input path is the risk we
   already got burned by.
3. **Hot reload.** Unloading a module that holds input is how incident 1 happened.
4. **The shell/filesystem/terminal tool surface.** Not needed; large attack surface.
5. **A second orchestrator.** `RunDirector` owns the loop.
6. **Their sandbox as our isolation story.** Same-world confinement is not isolation, and
   Frame Forge's problem has no fork boundary to fall back on.

---

## 10. Frame Forge safety boundary (unchanged, and how it survives any future dsh work)

```
┌──────────────────────────────────────────────────────────────────┐
│ planning / scenarios / (any future dsh agent)                      │
│   emit ToolRequest — declarative only, no OS calls                │
└───────────────────────────────┬──────────────────────────────────┘
                                │  struct only
┌───────────────────────────────▼──────────────────────────────────┐
│ InputPolicy  — target identity, allowlist, bounds, deadline,      │
│                approval, default-deny                            │
└───────────────────────────────┬──────────────────────────────────┘
                                │  validated
┌───────────────────────────────▼──────────────────────────────────┐
│ InputController — the ONLY component permitted to call           │
│                   SendInput. Mock / live / disabled.               │
└───────────────────────────────┬──────────────────────────────────┘
┌───────────────────────────────▼──────────────────────────────────┐
│ InputSafetyManager — hold registry, bounded holds, watchdog,      │
│   defensive release, deny-list, context-menu-key refusal,        │
│   input-language baseline/restore, target-integrity assertion,   │
│   event-level audit                                             │
└──────────────────────────────────────────────────────────────────┘
```

Enforced structurally, not by convention: CI asserts no `SendInput` outside
`safety.py`, and constructing an executor with a live port and no controller raises.

**A dsh integration, if ever approved, would sit strictly above the policy layer** — an
adapter translating dsh tool calls into `ToolRequest` and *nothing else*. It could not
reach the controller, because the controller's constructor requires a port that only the
runtime constructs, and the policy rejects any request without a verified target.

---

## 11. Phased roadmap

* **Phase 0 — research.** *This document.* No code touched.
* **Phase 1 — pattern adoption (Frame Forge only, no dsh dependency).**
  Fail-closed approval, pre-execution logging, monotonic policy ordering, orthogonal
  outcome fields, stuck-loop detector. All in `actions/` + `kernel/`. No new runtime deps.
* **Phase 2 — plugin seam, if and only if plug-and-play is actually needed.**
  Cordis-style `apply(ctx)` → disposers, one registry, exclusive input slot. Built by us.
* **Phase 3 — optional external integration.** Only if a real requirement appears, via an
  adapter above the policy layer, pinned to a released tag, in an isolated process.

**Not proposed:** migrating the orchestrator, adopting dsh plugins, or enabling its
computer-use providers.

---

## 12. POC (only if Phase 2/3 is ever approved)

Reversible by construction: an isolated branch `poc/dsh-adapter`, no production
credentials, no auto-approved shell, workspace-scoped filesystem only, no installers, no
system-setting changes, **no desktop keyboard/mouse injection** (the adapter emits
`ToolRequest` only), no modification of the existing harness, one synthetic workspace.

**Success:** an adapter turns a synthetic dsh-shaped tool call into a `ToolRequest`; policy
denies it for a non-allowlisted target; the denial is audited with a remedy; no OS input path
is reachable. **Failure:** any route to `SendInput`, or any approval that defaults open —
abort and delete the branch. **Rollback:** `git branch -D poc/dsh-adapter`.

---

## 13. Decision memo — what must NOT be adopted

1. **Any desktop-input provider or plugin.** Non-negotiable.
2. **Runtime dependency on `dsh`** in any tier, including "temporarily".
3. **Hot reload while input may be live.**
4. **Third-party plugins inside the input process.**
5. **A shell/filesystem/terminal surface for agents.**
6. **Their sandbox presented as an isolation guarantee.** It is same-world; we say so
   explicitly.
7. **Edits to dsh internals.** No clean extension point was needed for any recommendation
   above — all eight are patterns, not patches.

---

## 14. Honest limitations of this research

* I read READMEs, architecture docs and the tools/approval pipeline; I did **not** read the
  TypeScript implementation of `safety.py`-equivalent logic, the Cordis runtime internals,
  or the `experimental-computer-use-*` packages. Claims about *behaviour* are therefore
  sourced from maintainers' own documentation, which is authoritative for intent but not
  verified by execution.
* I did not install, build or run dsh. Nothing about its runtime behaviour is verified here.
* Package list is from the GitHub contents API at `master` on 2026-10-04 and will drift.
* The 243k-star figure is as-reported by the API and is **not** evidence of maturity; the
  repository was created ~7 weeks earlier. I weighted source over metrics throughout.
