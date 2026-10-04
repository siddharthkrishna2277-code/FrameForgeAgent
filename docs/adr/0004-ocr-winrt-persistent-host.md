# ADR 0004 - WinRT OCR via a persistent PowerShell host

**Status:** Accepted · **Date:** 2026-10-03

## Context

OCR is required for menu navigation. Options: Tesseract (separate binary install, no
cp314-friendly path, slow on CPU), RapidOCR + onnxruntime (~15 MB, extra), or the OS's own
`Windows.Media.Ocr`, verified working offline on the development machine with en-US and
en-GB installed.

The OS engine was clearly best — zero install, no model download, OS-tuned. It is WinRT,
though, which is async-only and awkward from Python.

The first implementation shelled out to PowerShell **once per frame**. Measured: **~1700 ms
per call**, of which ~578 ms is bare PowerShell process spawn and ~250 ms more resolving
the WinRT projection types. None of that is recoverable by caching, because it is paid per
process.

## Decision

Run a **persistent PowerShell host** that starts once and serves requests over
stdin/stdout, one image path per line and one JSON object per response.

## Consequences

**Measured result**

| Approach | Per-request | Notes |
|---|---|---|
| One-shot PowerShell | ~1700 ms | ~830 ms of it is fixed startup |
| Persistent host | ~36 ms steady state, ~169 ms mean | startup ~1.2 s, paid once |
| Full 1920x1080 frame | ~216 ms | WinRT recognise ~189 ms |
| Downscaled to 1280px longest edge | ~67 ms | WinRT recognise ~47 ms |

So ~10x faster, and downscaling before recognition is a further ~3x on full frames. The
adapter downscales to a configurable longest edge (default 1280) and scales the returned
boxes back to full-frame coordinates.

**Accepted costs**

- A resident PowerShell process must be supervised; if it dies, OCR degrades to
  unavailable and the run continues vision-only.
- A PowerShell execution policy could block the host. `doctor` reports this.
- Two WinRT projection pitfalls are handled and documented in the code: the `AsTask`
  generic-overload problem (needs reflection), and single-element collections being
  flattened to `Object[]` (needs explicit field coercion). Both produced silent failures
  first — zero-size bounding boxes and a type-conversion error — before being fixed.

**Rejected**

`winsdk` in-process bindings. The package is a beta with a heavy dependency tree that did
not install cleanly on the development machine (abandoned after 13 minutes). The PowerShell
host needs no dependency at all, and the in-process path is still attempted first when
`winsdk` is present, so the faster option is used if available.
