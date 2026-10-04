# Research round 2: the parts that change Frame Forge's design

Sources: `docs/protocol_v1.md` (the canonical internal spec), `execpolicy/README.md`,
`execpolicy/src/policy.rs`, `execpolicy/src/amend.rs`, `process-hardening/README.md`, the
`codex-rs/` directory listing, and `core/src/lib.rs`. Round 1 covered the workspace, the
agent loop, sandboxing and context rules.

---

## 1. `execpolicy` — a policy engine with a shape Frame Forge should copy

Rules are declared in **Starlark**, evaluated by longest-prefix match:

```starlark
prefix_rule(
    pattern = ["cmd", ["alt1", "alt2"]],   # ordered tokens; a list = alternatives
    decision = "prompt",                   # allow | prompt | forbidden
    justification = "why this rule exists",
    match    = [["cmd", "alt1"], "cmd alt2"],   # must match   (unit tests)
    not_match = [["cmd", "oops"]],               # must not match
)
host_executable(name = "git", paths = ["/usr/bin/git"])
```

Four properties worth taking verbatim:

1. **`justification` is mandatory in spirit** — "When `decision = 'forbidden'`, include a
   recommended alternative in the `justification`, e.g. *'Use `jj` instead of `git`'*." A
   refusal that does not say what to do instead is a dead end for the user.
2. **Rules carry their own tests.** `match` / `not_match` are examples validated *at load
   time*. Policy correctness becomes a load-time property, not a test-suite afterthought.
3. **Decision is an enum, and severity composes.** The effective decision is the strictest
   across all matches: `forbidden > prompt > allow`.
4. **No match is a distinct outcome** — `matchedRules: []`, `decision` omitted. Not an
   implicit allow.

### The finding that matters most

The README documents this, and it is easy to skim past:

> With no exact rule match, execpolicy **may** fall back from `/usr/bin/git` to basename
> rules for `git`. … If no `host_executable()` entry exists for a basename, **basename
> fallback is allowed**.

So `/tmp/evil/git status` can match a rule written for `git status`. And because the
amendment path (`blocking_append_allow_prefix_rule`) *appends to the policy file*, an
allow-rule learned once for a benign command becomes a prefix rule that later matches an
unrelated program with the same basename.

**Codex is honest that this is preview** ("execpolicy commands are still in preview; the
API may have breaking changes") and mitigates it two ways: basename fallback is opt-in
behind `--resolve-host-executables`, and `host_executable(name=..., paths=[...])` pins which
absolute paths may resolve.

The transferable lesson is not the bug. It is that **a policy keyed on a mutable, attacker-
influenced string needs an absolute-path identity component.** Frame Forge's analogue is
worse in one respect: the deny-list and allow-list key on *window class name and process
name*, both of which a process controls.

---

## 2. `protocol_v1.md` — the clearest statement of the agent model anywhere

This is the canonical internal spec, and it corrects a simplification I made in round 1.

### The entity ladder

| Entity | Meaning |
|---|---|
| `Model` | The Responses REST API |
| `Codex` | The engine. Background thread or separate process |
| `Session` | Current configuration + state. Created by `Op::ConfigureSession` |
| `Task` | `Codex` executing work in response to user input. **At most one at a time** |
| `Turn` | One request/response/execute cycle. **A `Turn` yielding no output terminates the `Task`** |

Three rules stated explicitly:

* `Session` has **at most one `Task`** running at a time. For parallelism, run one `Codex`
  per thread of work.
* A `Task` terminates when: the model completes with nothing to feed forward, new user
  input arrives (which **aborts** the current task), the UI interrupts, a fatal error
  occurs, or it is **blocked awaiting approval**.
* Reconfiguring the session **aborts any running execution**.

That last one is the detail I got wrong in Frame Forge. My `RunDirector` reconfigures the
focus target mid-run (on resume from a pause) and I had to think about it as a special
case. Codex treats it as a general invariant: **configuration change implies abort**. A
simplification I should adopt.

### The queue pair is the entire interface

> `Codex` communicates with UI via a **SQ (Submission Queue)** and **EQ (Event Queue)**.

* UI → Codex: `Submission`, carrying a UI-supplied `sub_id`. `Op` is the enum of all
  payloads, and it is **`non_exhaustive`**.
* Codex → UI: `Event`, each carrying the `sub_id` of the user turn that started the task.
  `EventMsg` is also `non_exhaustive`, and the spec says to expect new variants over time.

`non_exhaustive` on both directions is the compatibility posture: the core commits to the
*shape* of the channel, not to a closed set of messages.

**Correction to my round 1 report:** I described the outer loop as awaiting `Op` values
from "a submission channel", which is right, but I missed that the response direction is
equally first-class — a queue pair, not a request/response call. Frame Forge's `EventLog`
is append-only JSONL with no back-channel; the director polls `fetch_observation()`
directly. For an event-sourced system with a UI ahead of it, that is a real gap.

---

## 3. What changed in the workspace since my last look

`codex-rs/` now lists **120 directories** (round 1 read the Cargo workspace at ~150 members;
several are nested or newer). Names that were not there before:

`guardian-context` · `mxc-sandbox` · `windows-sandbox-service` · `voice-host` ·
`user-verification` · `realtime-webrtc` · `agent-message-board-client` ·
`attachment-store` · `otel-trace-websocket` · `tcp-tunnel` · `websocket-auth` ·
`mermaid` · `collaboration-mode-templates` · `config-schema`

Two readings worth flagging, both inference rather than fact:

* **Sandboxing is being split by platform into services** (`mxc-sandbox`,
  `windows-sandbox-service`). Windows sandboxing is the least granular of the three, per
  the `core/README.md`, and it is also the one that needs a persistent privileged component
  to manage sandbox users and ACLs. Extracting it looks like the cost of that being made
  real.
* **Multiple transport/auth crates** (`tcp-tunnel`, `websocket-auth`,
  `realtime-webrtc`, `stdio-to-uds`, `uds`) suggest remote and multi-surface operation is
  now a first-class concern rather than an extension. That is the same direction Frame
  Forge's roadmap §14 points for a future remote executor.

---

## 4. `process-hardening` — a crate I would not have guessed exists

```rust
pre_main_hardening()   // called via #[ctor::ctor], before main()
```

* disables core dumps
* disables `ptrace` attach on Linux and macOS
* **strips `LD_PRELOAD` and `DYLD_*` from the environment**

That last item is the interesting one. Those variables let any process inject code into
Codex's children. A local agent that shells out is, by default, a process that can be
subverted by anything that can set an environment variable — including, on a shared
machine, another user.

Frame Forge launches `notepad.exe` and PowerShell. It does **not** strip the environment it
inherits. `pre_main_hardening` is a two-dozen-line idea with no runtime cost, and it is
directly applicable: Frame Forge passes its environment straight through to a subprocess it
spawns.

---

## 5. Revised assessment for Frame Forge

### Adopt (high value, low cost)

| Codex pattern | Frame Forge action |
|---|---|
| `justification` required on every refusal, naming the alternative | Every policy denial should say what to do instead. Currently `DenyReason` + detail, with no remedy. |
| Rules carry their own `match` / `not_match` examples, validated at load | Validate the deny-list and allow-list against real windows at startup, not only in tests. |
| Severity composition: strictest wins | Frame Forge's deny reasons are first-match. Explicit severity ordering would be more predictable. |
| Config change aborts in-flight execution | Adopt as a general invariant, not a resume special-case. |
| `pre_main_hardening`: strip `LD_PRELOAD` / `DYLD_*` | Frame Forge spawns real processes and inherits its environment unchanged. |

### Fix in Frame Forge (from the execpolicy finding)

Frame Forge's target identity is *softer* than Codex's. Codex pins absolute executable
paths; Frame Forge keys on **window class name and process name**, and a process controls
both. A window can present class `Notepad` and run as `notepad.exe` while being something
else entirely.

That is not a theoretical concern for a system that operates on **screen pixels belonging to
a foreground window** — it is the same class of problem as the two incidents already fixed,
where a *legitimate* helper (`tasklist`, and our own console) turned out to be the thing
sending input.

Concrete hardening, in priority order:

1. **Verify the target image path**, not just the process name, for every live run.
   Codex already does this via `QueryFullProcessImageNameW` for exactly this reason.
2. **Refuse a target whose image path is not on disk** or whose basename and image path
   disagree.
3. Treat class-name and process-name agreement as *necessary but not sufficient*.

### Reconsider

**The queue pair.** `EventLog` plus synchronous polling works headlessly and is testable,
but it is not the shape a dashboard needs. Adopting Codex's SQ/EQ now — before the UI
exists — would be cheaper than retrofitting it, and `sub_id` correlation is exactly what
the report's step records reconstruct by hand today.

### Still not adopting

Capability-aware sandbox selection. Codex picks the strongest sandbox its platform offers
and documents the weaker one's limits. Frame Forge has an analogous choice (mss vs DXGI) but
treats it as a performance knob. The honest framing is that it is a *capability* knob, and
capture fidelity is a safety property: at 108 ms/frame a fast-moving target can be
mis-observed, which is a correctness issue, not just a speed one.
