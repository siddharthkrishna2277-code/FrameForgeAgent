# ADR 0001 - The control loop is deterministic; AI only proposes

**Status:** Accepted · **Date:** 2026-10-03

## Context

Frame Forge must be "AI-powered", but the development hardware (i5-8250U, 12 GB, Intel
UHD 620) cannot run a useful local model: a 7B class model yields ~2-4 tok/s and starves
perception, and a vision-language model is not viable at all. Any reasoning therefore has
to be remote, or absent.

That creates a risk beyond cost: an LLM in the control loop of an agent that moves a real
mouse and types real keys on a machine with one user. The failure mode is not just a wrong
move — it is a *confidently wrong QA verdict*, which is worse than no verdict at all.

## Decision

The control loop is a deterministic state machine. Exactly one slot in it — "propose the
next logical action" — is pluggable.

- The AI emits **logical actions**, never input primitives.
- Every proposal, from any planner, passes the same validator, compiler, budget ledger,
  focus guard and postcondition machinery.
- The AI has no write path to any verdict (see ADR 0008).
- `ai: off` is a complete, tested, supported mode.

## Consequences

**Accepted costs**

- The deterministic tier must be good enough to be useful on its own. This is real work:
  anchor graphs, reaction policies, step pre/postconditions, bounded probes. It is not a
  stub, and it must be maintained as a first-class agent.
- Some objectives genuinely need open-ended reasoning, and those are worse in Tier 0.
  Those scenarios degrade rather than complete.

**Accepted benefits**

- The system works with no API key and no network. Verified: 259 tests pass with the AI
  tier entirely absent, and the live proof-of-value run used `ai: off`.
- Safety reasoning lives in code, which is testable. "The prompt tells it not to" is not
  a control; a validator that rejects is.
- QA runs are reproducible. Plans are recorded and replayed.
- Cost is bounded and capped, not open-ended.

**How this would be reversed**

Not reversibly, without redesign. The invariant "AI cannot emit a primitive" is structural.
