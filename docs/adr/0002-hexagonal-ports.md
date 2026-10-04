# ADR 0002 - Hexagonal architecture with ports over every Windows API

**Status:** Accepted · **Date:** 2026-10-03

## Context

Frame Forge depends on a large surface of Windows-specific machinery: screen capture,
window enumeration, focus tracking, session state, synthetic input, and OCR. That surface
is exactly where bugs are expensive — a focus bug sends clicks to the wrong window; a
coordinate bug clicks the wrong monitor.

Testing it requires either a real display on every test run, or a seam.

## Decision

Every Windows capability sits behind a port in `frameforge/ports/`. Nothing in `kernel`,
`actions`, `perception`, `planning`, `tasks` or `qa` imports a Win32 symbol. Each port has
at least one real adapter and one fake.

`frameforge simulate` runs the *real* director, assembler, compiler, executor, verifier and
report builder with fakes substituted. It is not a mock harness.

## Consequences

**Accepted costs**

- Indirection everywhere; more code than the obvious design.
- Discipline must be enforced, or the boundary erodes. Guardrail `G-DEV-01` and
  `TEST-ROLE-02` assert it mechanically rather than trusting review.

**Accepted benefits**

- The full test suite runs headless in ~140 s, on any machine, with no display.
- The multi-monitor coordinate bug class is testable — the second-monitor offset is a
  unit test, not a manual check.
- A future remote Executor is a transport change, not a redesign.
- Optional dependencies degrade capability instead of crashing.

**Measured evidence**

`tests/hardware` exercises the real adapters (13 tests, all passing on the development
machine), while `tests/unit` + `tests/integration` (259 tests) run with no display at all.
