"""Author profile landmarks from a real window, using real evidence.

Instead of guessing what text a foreign application displays, capture it and let the OCR
tell us. This is the reconnaissance step for writing a profile against software Frame
Forge did not build, and it is the step most people would otherwise get wrong.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import numpy as np

from frameforge.adapters.capture.mss_capture import MssCaptureSource
from frameforge.adapters.ocr.winrt_ocr import WinRtOcr
from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
from frameforge.ports.capture import Surface, SurfaceKind


def main() -> int:
    win = PyWin32WindowAdapter()
    print("launching a fresh notepad...")
    proc = subprocess.Popen([r"C:\Windows\System32\notepad.exe"])
    time.sleep(3.0)

    # Prefer the *unsaved* document: a scenario must never risk the user's open file.
    target = None
    for w in win.enumerate_windows():
        if w.class_name == "Notepad" and w.is_visible and "Untitled" in w.title:
            target = w
            break
    if target is None:
        for w in win.enumerate_windows():
            if w.class_name == "Notepad" and w.is_visible:
                target = w
    if target is None:
        print("notepad window not found")
        return 1
    win.set_foreground(target.hwnd)
    time.sleep(0.8)
    print(f"  window: {target.describe()}")
    print(f"  client: {target.client_rect.as_tuple()} origin={target.client_origin_screen.as_tuple()}")

    client = target.client_rect.size
    surface = Surface(
        kind=SurfaceKind.WINDOW,
        size=client,
        offset_x=target.client_origin_screen.x,
        offset_y=target.client_origin_screen.y,
        hwnd=target.hwnd,
    )
    cap = MssCaptureSource()
    cap.open(surface)
    frame = cap.grab()
    cap.close()
    print(f"  captured {frame.array.shape} in {frame.capture_ms:.0f}ms")

    out = Path("evidence_samples")
    out.mkdir(exist_ok=True)
    import cv2

    (out / "notepad_client.png").write_bytes(cv2.imencode(".png", cv2.cvtColor(frame.array, cv2.COLOR_RGB2BGR))[1].tobytes())

    # Mean colour of bands, so landmarks can be placed by geometry rather than by eye.
    h, w = frame.array.shape[:2]
    print()
    print("  horizontal bands (mean luminance):")
    for i in range(0, h, max(1, h // 12)):
        band = frame.array[i : i + max(1, h // 12)]
        print(f"    y={i:4}..{min(h, i + h // 12):4}  luma={band.mean():6.1f}  rgb={band.reshape(-1,3).mean(axis=0).round(0)}")

    print()
    print("  OCR of the full client area:")
    ocr = WinRtOcr()
    if not ocr.capabilities().available:
        print("    OCR unavailable:", ocr.capabilities().notes)
        return 1
    t0 = time.perf_counter()
    result = ocr.read(frame.array)
    ms = (time.perf_counter() - t0) * 1000
    print(f"    engine={result.engine} ms={ms:.0f} lines={len(result.lines)} err={result.error}")
    print()
    print("    norm_x  norm_y  w     h     conf  text")
    for line in result.lines:
        r = line.rect
        nx = r.x / max(1, w)
        ny = r.y / max(1, h)
        nw = r.width / max(1, w)
        nh = r.height / max(1, h)
        print(f"    {nx:.3f}  {ny:.3f}  {nw:.3f} {nh:.3f} {line.confidence:.2f}  {line.text!r}")

    print()
    print("  OCR of just the top 60px (the menu bar band):")
    strip = frame.array[:60, :, :]
    r2 = ocr.read(strip)
    for line in r2.lines:
        print(f"    y={line.rect.y} x={line.rect.x} {line.text!r}")
    ocr.close()

    print()
    print("  saved evidence_samples/notepad_client.png")
    try:
        proc.terminate()
    except Exception:
        pass
    print("PROBE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())