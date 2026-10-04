# ADR 0003 - mss is the default capture backend; DXGI is opt-in

**Status:** Accepted · **Date:** 2026-10-03

## Context

Two capture paths exist: DXGI Desktop Duplication (GPU-backed, lower latency) and BitBlt
via `mss` (~1 MB, works everywhere including RDP and some VM display drivers). DXGI is
sensitive to driver quirks, and an early phase of this project must not be blocked on one.

## Decision

`mss` is the default. DXGI is an optional extra (`dxcam`), selected by configuration, and
falls back to mss with an explicit warning event rather than an exception.

## Consequences

**Accepted costs — measured on the development machine**

Capture is **~108 ms per 1920x1080 frame (~9 fps)**. This is materially slower than the
<25 ms originally projected in `docs/ROADMAP.md` §2. Consequences, stated honestly:

- A decision loop runs at roughly 4-8 Hz including perception, not 20-40 Hz.
- This is adequate for menu navigation and QA scenarios — which is what the product
  actually does — and inadequate for anything requiring frame-tight reaction (racing lines,
  twitch aiming). Those need DXGI and still might not be enough.
- A full-resolution frame is ~6 MB; a bounded ring and no per-epoch storage are required on
  12 GB of RAM.

**Accepted benefits**

- Nothing in P0-P5 was ever blocked by a capture problem.
- Backend promotion is a config flag, already implemented and tested.

**Honest note**

The ROADMAP's §2 performance table still shows the original <25 ms target. That target was
wrong for BitBlt on this hardware, and the measured number is the one that matters. §2 of
the roadmap should be read with this ADR.
