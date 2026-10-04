"""Isolated OCR verification. Slower than the full probe so it can be run on its own.

Usage: .venv/Scripts/python scripts/probe_ocr.py
"""
from __future__ import annotations

import sys
import time

import numpy as np

sys.path.insert(0, "src")

from frameforge.adapters.ocr.winrt_ocr import WinRtOcr


def main() -> int:
    import cv2

    print("initialising WinRtOcr (this probes both transports)...")
    t0 = time.perf_counter()
    ocr = WinRtOcr()
    init_s = time.perf_counter() - t0
    caps = ocr.capabilities()
    print(f"  init took {init_s:.1f}s")
    print(f"  available = {caps.available}")
    print(f"  engines   = {caps.engines}")
    print(f"  transport = {ocr.transport}")
    print(f"  languages = {caps.languages}")
    print(f"  notes     = {caps.notes}")

    if not caps.available:
        print("\nOCR UNAVAILABLE - text assertions would evaluate to UNKNOWN")
        return 1

    # Render a few known strings and check each is found.
    cases = [
        ("PLAY", (255, 255, 255), (0, 0, 0)),
        ("SETTINGS", (20, 20, 30), (230, 230, 240)),
        ("MAIN MENU", (10, 10, 10), (255, 255, 255)),
    ]
    print()
    ok = 0
    for text, bg, fg in cases:
        canvas = np.zeros((90, 700, 3), np.uint8)
        canvas[:, :] = bg
        cv2.putText(canvas, text, (30, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.6, fg, 4)
        t0 = time.perf_counter()
        result = ocr.read(canvas)
        ms = (time.perf_counter() - t0) * 1000
        texts = [line.text for line in result.lines]
        hit = any(text.lower() in " ".join(texts).lower() for _ in (0,))
        ok += hit
        print(f"  {text!r:14} -> {ms:6.0f}ms  engine={result.engine}")
        print(f"     lines={len(result.lines)}  found={hit}")
        for line in result.lines:
            print(f"       {line.text!r} @ {line.rect.as_tuple()}")

    print()
    print(f"  {ok}/{len(cases)} rendered strings recovered")

    # Second read should hit the cache-free path again; measure steady-state cost.
    canvas = np.zeros((90, 700, 3), np.uint8)
    canvas[:, :] = (0, 0, 0)
    cv2.putText(canvas, "NEW GAME", (30, 62), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
    t0 = time.perf_counter()
    r = ocr.read(canvas)
    print(f"  steady-state read: {(time.perf_counter()-t0)*1000:.0f}ms  lines={len(r.lines)}")
    print(f"  text: {r.text!r}")

    print()
    print("OCR PROBE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())