# ADR 0008 - The AI has no write path to any verdict

**Status:** Accepted · **Date:** 2026-10-03

## Context

Frame Forge's purpose is telling a developer whether a test passed. The consumer of a
report is a coding agent that will open the game project and decide what to fix. That makes
a confident wrong verdict more damaging than no verdict: it sends someone to the wrong file
with false confidence.

Most naive LLM-agent designs put the model somewhere near the conclusion, which is exactly
where it must not be.

## Decision

`PASS` / `FAIL` / `UNKNOWN` are produced **exclusively** by the deterministic `Verifier`
evaluating a profile-defined assertion against a captured `Observation`.

Enforced structurally:

- `TEST-VERD-01` parses `perception/verify.py` and asserts it imports no planning or AI
  module. **This is a release gate.**
- `UNKNOWN` may never be coerced to `PASS`. There is no "assume passed on timeout" path,
  and a test asserts the phrase never appears as a call in the module.
- Every verdict binds to the frame hash and index it came from.
- A missing observation yields `UNKNOWN`, never `PASS`.
- AI narrative lives in `report.narrative_ai`, outside `report.verdicts`, and is labelled.
- A run that ends paused reports `UNKNOWN`, not `COMPLETED` — a false green is the worst
  possible output.

## Consequences

**Accepted costs**

- Genuinely ambiguous states produce `UNKNOWN`, which is less satisfying than a verdict.
  This is the intended trade: for a QA tool, "I could not determine this" is more useful
  than a guess.
- Perception quality now bounds QA quality. That is a real dependency, and it is why the
  stability criteria include a measured OCR accuracy check.

**Verified**

`TEST-VERD-01` is green. The canary scenario reports `FAIL` for a planted defect and
`PASS` without it, with the failure evidence screenshot independently confirming the defect.
