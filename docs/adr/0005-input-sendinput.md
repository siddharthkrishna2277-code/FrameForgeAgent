# ADR 0005 - Raw `SendInput` via ctypes, not a convenience library

**Status:** Accepted · **Date:** 2026-10-03

## Context

Input is the highest-consequence surface in the system: a bug here moves a real user's
mouse and types real keys. The layer must be predictable about ordering, must support
hold/release lifetimes, and must be able to guarantee "no input reached the OS".

PyAutoGUI was considered and rejected: no cp314 wheel (sdist only), slower, and it cannot
express the relative-mouse or hold semantics the executor relies on.

## Decision

A single adapter over Win32 `SendInput` via ctypes, with the structures declared
explicitly. Window management uses pywin32.

Three invariants:

1. The enabled gate is checked **inside every send**, not at a higher layer, so an estop is
   effective even against already-queued work.
2. Absolute coordinates are virtual-desktop pixels normalised across the **whole** virtual
   desktop with `MOUSEEVENTF_VIRTUALDESK`. Using per-monitor coordinates on this machine's
   1920+1920 layout sends every click to the wrong screen.
3. `release_all()` works while disarmed. A disarm must not prevent cleanup.

## Consequences

**Accepted costs**

- Hand-written ctypes, with the usual sharp edges: `BOOL` returns truncate under the
  default `int` restype unless declared, and `GetTickCount` lives in kernel32, not user32.
  Both were hit and are now declared explicitly with comments explaining why.
- The `KEYEVENTF_EXTENDEDKEY` set must be maintained; without it, arrow keys arrive as
  numpad arrows and right-ctrl as right-alt.
- Gamepad input is a declared no-op in this adapter (a P5 extension), recorded rather than
  silently pretended.

**Verified**

`tests/hardware/test_hil.py` moves the real cursor and reads it back through
`GetCursorPos`, on both monitors, and asserts estop releases held state in under 100 ms.
All 13 hardware tests pass.
