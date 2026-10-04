# Research: how OpenAI Codex is built

Primary sources read directly, not summarised from blog posts:
`AGENTS.md`, `codex-rs/Cargo.toml`, `codex-rs/core/src/lib.rs`, `codex-rs/core/README.md`,
`codex-rs/core/src/tools/mod.rs`, `docs/config.md`, `docs/exec.md`, `docs/sandbox.md`.
Secondary: DeepWiki and two engineering write-ups, used only to locate files.

---

## 1. Shape of the repository

`openai/codex`, Apache-2.0. Implementation is Rust, edition 2024, in one Cargo workspace
under `codex-rs/`. Crate directories are short (`core/`), crate names are prefixed
(`codex-core`). The workspace lists **~150 crates** — most of them narrow utilities.

The interesting part is not the crate count; it is that the count is the *response to a
documented failure mode*:

> `codex-core` has become bloated because it is the largest crate, so it is often easier
> to add something new to `codex-core` than to refactor out the library code... **resist
> adding code to codex-core!** — `AGENTS.md`

> Keep crate API surfaces as small as possible. Avoid proliferating test-only helpers.
> — `AGENTS.md`

That is a codebase documenting a gravitational pull it now actively pushes back against.
Worth noting as a design lesson: the anti-pattern is acknowledged *in the repository*, in
the file every agent reads first.

### The crates that matter

| Crate | Responsibility |
|---|---|
| `codex-core` | agent loop, sessions, tool routing, context management, sandbox manager |
| `codex-tools` | tool definitions and execution |
| `codex-protocol` | wire types shared by every surface |
| `codex-app-server` | JSON-RPC server for IDEs and extensions |
| `codex-tui` | terminal UI (Ratatui) |
| `codex-exec` / `codex-exec-server` | headless, scripted runs |
| `codex-sandboxing` | platform-neutral sandbox policy abstraction |
| `codex-linux-sandbox` | Bubblewrap + Landlock + seccomp |
| `codex-windows-sandbox` | restricted tokens, ACLs, sandbox users, firewall |
| `codex-execpolicy` | command policy engine |
| `codex-hooks` | lifecycle hook execution |
| `codex-rollout` / `codex-rollout-trace` | session recording and replay |
| `codex-mcp` | Model Context Protocol |

---

## 2. The agent loop

Three nested layers, and the naming matters because each has a different lifetime:

1. **`submission_loop`** — the outermost infinite loop. Awaits `Op` values from a
   submission channel and dispatches them. Runs for the entire session; terminates only on
   `Op::Shutdown`.
2. **Turn loop** — entered when the submission loop receives `Op::UserInput`. Assembles the
   prompt (system instructions + tool definitions + history), calls the Responses API, and
   consumes the event stream.
3. **Tool loop** — within one turn, tool calls and their results are appended to the
   prompt and fed back to the model until a `done` event ends the turn.

From `core/src/lib.rs`, the internal structure is roughly:
`CodexThread` owns the submission queue and event emission; an internal `Session`
coordinates prompt building, turn execution and tool invocation; `ContextManager` handles
history, normalisation and **compaction**; `ModelClient` handles streaming, retries and
protocol.

### Turn inputs are a typed enum, not a free-form message

`core/src/lib.rs` re-exports the turn-input vocabulary:
`TurnInput`, `TurnInputRequest`, `TurnInputSubmission`, `StartIfIdleSubmission`,
`SteerSubmission`, `SuspendTurnOutcome`, `RecoverTurnRequest`, `NotSubmittedReason`.

This is the single most transferable idea in the repository. A user turn is not a string
that gets appended; it is one of several *operations*, each with different semantics about
when it may start a turn, whether it interrupts one, and what happens if it cannot.
`NotSubmittedReason` exists because "I refused to accept your input" needs to be a
first-class, reportable outcome rather than a silent drop.

---

## 3. The tool boundary

Tools are declared through a `ToolSpec` enum, and every invocation is sandboxed.

Execution output is formatted for the model deliberately — from
`core/src/tools/mod.rs`:

```rust
sections.push(format!("Exit code: {}", exec_output.exit_code));
sections.push(format!("Wall time: {duration_seconds} seconds"));
if total_lines != formatted_output.lines().count() {
    sections.push(format!("Total output lines: {total_lines}"));
}
sections.push(truncate_text(&content, truncation_policy));
```

Three properties worth stealing:

* **exit code and wall time are always present**, so the model is never guessing about
  whether a command succeeded;
* **truncation is explicit and reported** — the pre-truncation line count is stated, so
  truncation is never invisible;
* **timeouts are first-class text**, not an exception: `command timed out after {} ms`
  followed by whatever output existed.

---

## 4. Sandboxing and approval — the part most relevant to Frame Forge

The critical design decision: **the main process runs unsandboxed. Only spawned commands
are confined.** The agent itself is trusted; its *effects* are contained.

Policy modes: `ReadOnly` · `WorkspaceWrite` · `DangerFullAccess` · `externalSandbox`.

Platform implementations:

| Platform | Mechanism |
|---|---|
| macOS | Seatbelt (`sandbox-exec`) — the App Store sandbox, applied per command |
| Linux | Bubblewrap for namespaces + Landlock + seccomp for syscall filtering |
| Windows | Restricted tokens, ACLs, dedicated sandbox users, firewall rules |

### Windows detail — the most interesting part

Two backends, with honestly documented trade-offs:

**Restricted-token backend** (`CreateRestrictedToken`) — supports legacy `ReadOnly` and
`WorkspaceWrite`. Cannot enforce explicit deny (`none`) carveouts or reopened writable
descendants.

**Elevated sandbox** — dedicated local accounts (`CodexSandboxOffline`,
`CodexSandboxOnline`), ACLs written via `add_deny_read_ace` / `add_deny_write_ace`, and
firewall rules. Exact readable/writable roots, platform defaults, carveouts.

The `core/README.md` states the limitations of the cheaper backend outright rather than
implying parity. That is a good pattern: a weaker mechanism with a named weakness is more
useful than a uniform abstraction that hides one.

The escalation ladder from `core/src/exec.rs` is a `SandboxAttempt` — the Rust-level
decision — which the Windows backend then translates into user identity, ACLs, firewall
rules and process tokens. As one write-up puts it: *Windows does not understand a Rust
enum named `SandboxAttempt`. It understands users, access-control entries, restricted
tokens, firewall rules, pipes, process handles and exit statuses.*

There is also a hook-driven **Guardian** approval subsystem, and an `execpolicy` engine
that can amend policy (e.g. skip future approvals for similar commands) after a failure.

---

## 5. Context management

`AGENTS.md` has the sharpest rules in the repo:

1. **No history rewrite** — context is built incrementally.
2. **Avoid frequent context changes that cause cache misses.**
3. **No unbounded items** — everything injected has a bounded size and a hard cap.
4. **No items larger than 10K tokens.**
5. New individual items over ~1k tokens are flagged **P0** and need manual review.
6. All injected fragments must be a struct in `core/context` implementing
   `ContextualUserFragment`.

Rule 6 is the mechanism that makes the others enforceable: a new thing cannot simply
concatenate a string into the prompt. It must become a typed fragment in one place.

---

## 6. App server protocol

JSON-RPC 2.0, and the type discipline is unusually strict:

* RPC methods are `<resource>/<method>`, resource singular (`thread/read`, `app/list`).
* Fields camelCase on the wire; **config payloads are the snake_case exception**, to mirror
  `config.toml`.
* Timestamps are integer Unix seconds, named `*_at`.
* For new list methods, **cursor pagination is the default**, not optional.
* Discriminated unions use explicit tagging in *both* serialisers: `serde` and `ts`.
* Experimental surface is marked with `#[experimental("method/or/field")]`.

Tiering: **Thread** (whole conversation) → **Turn** (one exchange) → **Item** (atomic
event), each with `started` / `completed` / `delta` lifecycle notifications.

---

## 7. Engineering discipline worth noting

* **Change size is capped.** ≤800 changed lines unless mechanical; ≤500 for complex logic.
  Larger work must be staged, with the smallest coherent stage landed first.
* **Module size is capped.** Target <500 LoC excluding tests; above ~800 LoC, add a new
  module instead of growing an existing one — with named offenders listed explicitly
  (`tui/src/app.rs`, `chatwidget.rs`, `bottom_pane/*`).
* **Integration tests preferred over unit tests for agent changes** (`core/suite`, using
  `test_codex`). Features that change agent logic *must* add one.
* "Do not add tests for values that are statically defined." "Do not add negative tests for
  logic that was removed."
* `unwrap_used`, `expect_used`, `await_holding_lock`, and ~30 other lints are **deny** at
  workspace level.
* "Be patient with Rust commands and never try to kill them using the PID. Rust lock can
  make execution slow; this is expected." — a warning to agents that the temptation to
  kill a build is itself a failure mode.
* Builds with Bazel as well as Cargo; `MODULE.bazel.lock` drift is a CI failure.

---

## 8. What transfers to Frame Forge

Stated as judgement, not as fact about Codex.

| Codex pattern | Frame Forge status |
|---|---|
| Model emits structured requests; a constrained executor acts | **Adopted** — `ToolRequest` → `InputPolicy` → `InputController` |
| Typed input enum, not a free-form message | **Adopted** — `Action` union, `RunState`, `Disposition` |
| Main process trusted, *effects* contained | Adopted — policy layer guards all effects |
| Deny by default, widen only with explicit approval | **Adopted** — default-deny policy, `--refocus` opt-in |
| Exit code + wall time always reported | **Adopted** — `ToolResult`, per-verdict timings |
| Truncation explicit and stated | **Partly** — run artefacts are bounded, but no token-budget equivalent |
| Named weaknesses rather than uniform abstractions | Adopted — `docs/STABILITY.md` lists what is unproven |
| Hard caps on context items and change size | **Not adopted** — worth adding |

### The gap that matters most

Codex's biggest structural advantage for this class of problem is that **it barely needs
synthetic input**. It works through files, subprocesses and terminals. Frame Forge cannot
avoid the mouse and keyboard, which is precisely why it needed the two incidents to
discover that the problem was *"where does injected input land?"* rather than *"is the
mouse press correct?"*

The lesson is not "adopt Rust" or "adopt Seatbelt". It is:

> An agent that must synthesise input is solving a harder problem than an agent that does
> not — and the extra work is entirely in the containment layer, not the reasoning layer.

Codex puts its effort into sandboxing. Frame Forge now puts its effort into the
equivalent: target validation, a deny-list, bounded holds, guaranteed release and an audit
trail. Same shape, different substrate.

### Two things Codex has that Frame Forge does not

1. **Context-budget discipline** — hard caps on what enters the model's context, enforced
   by a typed-fragment trait. Frame Forge's AI packet is small and redacted, but nothing
   enforces a ceiling as a matter of policy.
2. **Capability-aware sandbox selection** — Codex knows its own security model on each
   platform and picks the strongest available. Frame Forge knows capture is weaker than
   DXGI but does not degrade its posture based on that.
