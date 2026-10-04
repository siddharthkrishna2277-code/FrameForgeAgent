"""Measure the persistent OCR host against the one-shot bridge.

Usage: .venv/Scripts/python scripts/probe_ocr_host.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")

from frameforge.adapters.ocr.winrt_host import OcrHostError, PersistentOcrHost


def main() -> int:
    import cv2

    canvas = np.zeros((200, 900, 3), np.uint8)
    canvas[:, :] = (12, 12, 16)
    cv2.putText(canvas, "PLAY", (40, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 4)
    cv2.putText(canvas, "SETTINGS", (40, 155), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (230, 230, 230), 3)

    tmp = Path(tempfile.gettempdir()) / "ffhost_probe.png"
    ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    tmp.write_bytes(buf.tobytes())

    print("starting persistent host (one-time cost)...")
    t0 = time.perf_counter()
    host = PersistentOcrHost(startup_timeout_s=45.0)
    startup_ms = (time.perf_counter() - t0) * 1000
    print(f"  startup: {startup_ms:.0f}ms  ready={host.ready}  engine={host.engine}")
    if not host.ready:
        print("  host failed to start")
        return 1

    print()
    print("per-request costs (host already warm):")
    wall_times = []
    for i in range(5):
        t0 = time.perf_counter()
        try:
            payload = host.recognize_path(tmp)
        except OcrHostError as exc:
            print(f"  request {i + 1}: ERROR {exc}")
            break
        ms = (time.perf_counter() - t0) * 1000
        wall_times.append(ms)
        lines = payload.get("lines", [])
        texts = [l["text"] for l in lines]
        print(f"  request {i + 1}: wall={ms:6.0f}ms  winrt={payload.get('recognize_ms')}ms  "
              f"lines={len(lines)}  {texts}")
        for l in lines:
            print(f"      {l['text']!r} @ ({l['x']},{l['y']},{l['w']},{l['h']})")

    if wall_times:
        mean = sum(wall_times) / len(wall_times)
        print()
        print(f"  mean wall per request: {mean:.0f}ms")
        print(f"  mean winrt recognise: {host.mean_recognize_ms:.0f}ms")
        print()
        print(f"  one-shot bridge was ~1700ms; persistent host is ~{mean:.0f}ms")
        print(f"  speedup: {1700 / mean:.1f}x")
        print()
        print(f"  effective rate: {1000 / mean:.1f} reads/sec")

    # Now the realistic case: a full 1920x1080 frame, downscaled vs not.
    print()
    print("full-resolution frame (1920x1080), which is what a real run captures:")
    full = np.zeros((1080, 1920, 3), np.uint8)
    full[:, :] = (30, 30, 36)
    cv2.putText(full, "NEW GAME", (700, 540), cv2.FONT_HERSHEY_SIMPLEX, 3.0, (255, 255, 255), 5)

    for label, img in (("full 1920x1080", full), ("downscaled 960x540", cv2.resize(full, (960, 540), interpolation=cv2.INTER_AREA))):
        p = tmp.with_name(f"ffhost_{label.replace(' ', '_').replace('x', '_')}.png")
        ok2, b2 = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        p.write_bytes(b2.tobytes())
        t0 = time.perf_counter()
        payload = host.recognize_path(p)
        ms = (time.perf_counter() - t0) * 1000
        texts = [l["text"] for l in payload.get("lines", [])]
        print(f"  {label:22} wall={ms:6.0f}ms winrt={payload.get('recognize_ms')}ms lines={len(texts)} {texts}")
        if payload.get("error"):
            print(f"      ERROR: {payload['error']}")
        p.unlink(missing_ok=True)

    host.close()
    tmp.unlink(missing_ok=True)
    print()
    print("HOST PROBE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())