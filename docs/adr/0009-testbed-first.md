# ADR 0009 - Build our own instrumented target before any real game

**Status:** Accepted · **Date:** 2026-10-03

## Context

A game-automation system cannot be developed credibly against "whatever game happens to be
installed". Bugs need to be seeded deterministically, perception regressions need a target
whose state changes in known ways, the suite must run where no game is installed, and a CI
runner has no display.

## Decision

Write the testbed: a ~450-line Tkinter application with a menu, an animated loading
screen, a gameplay view with a live HUD, a modal dialog, and a **seeded deterministic
defect** (the health bar is not rendered when HP < 30).

## Consequences

**Accepted benefits**

- On-demand, reproducible ground-truth bugs. The proof of value is objective: the canary
  scenario must `FAIL` with the defect present and `PASS` without it. Both verified on the
  development machine.
- Regression coverage that does not depend on game availability or licensing.
- A fixture for every later phase, including the guardrail fuzz corpus.
- The testbed is the authorised-signal seam demonstrated on our own application.

**Accepted costs**

- ~450 lines of code that is not part of the product.
- It is deliberately plain: solid fills, large high-contrast text, no alpha. Making it easy
  to perceive in unrepresentative ways would undermine its value as a fixture.

**Verified**

- `testbed_smoke`: **5/5 PASS** against the live window, with real capture, real WinRT OCR
  and real `SendInput`.
- `testbed_healthbar_bug` with `--bug healthbar`: **FAIL**, divergence at the health bar step.
- `testbed_healthbar_bug` without the bug: **PASS**.
- Evidence screenshot independently inspected: HP 0, no health bar, no HEALTHBAR label,
  minimap present — matching the verdict exactly.
