"""Verify the real Windows adapters on this machine. Not part of the test suite:
this touches the actual desktop, so it is opt-in hardware-in-the-loop work.

Usage: .venv/Scripts/python scripts/probe_hardware.py
"""
from __future__ import annotations

import sys
import time

import numpy as np

sys.path.insert(0, "src")

from frameforge.adapters.capture.mss_capture import MssCaptureSource
from frameforge.adapters.ocr.rapidocr_ocr import build_ocr
from frameforge.adapters.vision.opencv_vision import OpenCvVision
from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
from frameforge.ports.capture import Surface, SurfaceKind
from frameforge.ports.geometry import Rect, Size


def main() -> int:
    print("=" * 68)
    print("WINDOW / MONITORS / SESSION")
    print("=" * 68)
    win = PyWin32WindowAdapter()
    topo = win.monitors()
    for m in topo.monitors:
        print(f"  monitor[{m.index}] {m.device_name} {m.rect.as_tuple()} primary={m.is_primary}")
    print(f"  virtual={topo.virtual_rect.as_tuple()} primary_index={topo.primary_index}")
    print(f"  signature={topo.signature}")
    print(f"  session={win.session_state()} console_sid={win.console_session_id()}")
    print(f"  foreground={win.foreground().describe() if win.foreground() else None}")
    print(f"  idle_ms={win.idle_ms()}")

    print()
    print("  top windows:")
    for info in win.enumerate_windows()[:12]:
        if info.is_visible and info.title:
            print(f"    {info.describe()} client={info.client_rect.as_tuple() if info.client_rect else None}")

    print()
    print("=" * 68)
    print("CAPTURE (mss)")
    print("=" * 68)
    primary = topo.monitor(topo.primary_index)
    surface = Surface(
        kind=SurfaceKind.MONITOR,
        size=Size(primary.width, primary.height),
        offset_x=primary.rect.x,
        offset_y=primary.rect.y,
        monitor_index=primary.index,
        label=primary.device_name,
    )
    print(f"  surface={surface.label} size={surface.size.as_tuple()} offset=({surface.offset_x},{surface.offset_y})")
    cap = MssCaptureSource()
    cap.open(surface)
    times = []
    for i in range(12):
        f = cap.grab()
        if f is not None:
            times.append(f.capture_ms)
    print(f"  frames grabbed: {len(times)}")
    if times:
        print(f"  capture_ms: min={min(times):.1f} max={max(times):.1f} avg={sum(times)/len(times):.1f}")
        print(f"  status={cap.status().health} fps={cap.status().fps_estimate}")
        last = cap.grab()
        print(f"  frame shape={last.array.shape} dtype={last.array.dtype} hash={last.content_hash[:16]}")
        mean_rgb = last.array.reshape(-1, 3).mean(axis=0)
        print(f"  mean RGB={mean_rgb.round(1)} (proves RGB order, not BGRA)")
        cap.close()

    print()
    print("=" * 68)
    print("SECOND MONITOR CAPTURE")
    print("=" * 68)
    if len(topo.monitors) > 1:
        sec = topo.monitor(1)
        s2 = Surface(
            kind=SurfaceKind.MONITOR,
            size=Size(sec.width, sec.height),
            offset_x=sec.rect.x,
            offset_y=sec.rect.y,
            monitor_index=sec.index,
            label=sec.device_name,
        )
        cap2 = MssCaptureSource()
        cap2.open(s2)
        f2 = cap2.grab()
        print(f"  monitor[1] frame={f2.array.shape if f2 is not None else None} "
              f"mean={f2.array.mean().round(1) if f2 is not None else None}")
        cap2.close()
    else:
        print("  only one monitor")

    print()
    print("=" * 68)
    print("VISION (opencv)")
    print("=" * 68)
    vis = OpenCvVision()
    a = np.zeros((100, 100, 3), np.uint8)
    b = a.copy()
    b[40:60, 40:60] = 255
    print(f"  diff(a,a)={vis.diff(a, b).score:.4f}  (expect 0.0)")
    print(f"  diff(a,b)={vis.diff(a, b).score:.4f}  (expect ~0.04)")
    print(f"  similarity(a,b)={vis.similarity(a, b):.4f}")
    print(f"  is_black(zeros)={vis.is_black(a)}  is_black(rand)={vis.is_black(np.full((50,50,3),120,np.uint8))}")
    probe = vis.probe_color(np.full((40, 40, 3), (255, 0, 0), np.uint8), __import__(
        "frameforge.ports.vision", fromlist=["ColorProbe"]).ColorProbe.from_rgb(255, 0, 0))
    print(f"  red probe on red: ratio={probe.ratio:.3f} matched={probe.matched}")
    probe2 = vis.probe_color(np.full((40, 40, 3), (0, 255, 0), np.uint8), __import__(
        "frameforge.ports.vision", fromlist=["ColorProbe"]).ColorProbe.from_rgb(255, 0, 0))
    print(f"  red probe on green: ratio={probe2.ratio:.3f} matched={probe2.matched}  (expect ~0)")
    print(f"  activity(flat)={vis.region_activity(a):.5f}")
    noisy = (np.random.default_rng(0).integers(0, 255, (100, 100, 3))).astype(np.uint8)
    print(f"  activity(noisy)={vis.region_activity(noisy):.5f}  (expect >> flat)")
    tpl = noisy[20:40, 20:40]
    m = vis.match_template(noisy, tpl, threshold=0.99)
    print(f"  template self-match score={m.score:.4f} rect={m.rect.as_tuple() if m.rect else None}")
    small = vis.downscale(noisy, 64)
    print(f"  downscale 100x100 -> {small.shape}")

    print()
    print("=" * 68)
    print("OCR")
    print("=" * 68)
    ocr = build_ocr("auto")
    caps = ocr.capabilities()
    print(f"  engine={caps.primary} available={caps.available} languages={caps.languages}")
    print(f"  notes={caps.notes}")
    if caps.available:
        canvas = np.zeros((60, 400, 3), np.uint8)
        canvas[:, :] = (255, 255, 255)
        try:
            import cv2
            cv2.putText(canvas, "PLAY", (20, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 3)
            res = ocr.read(canvas)
            print(f"  OCR result ok={res.ok} engine={res.engine} lines={len(res.lines)} ms={res.mono_ms:.0f}")
            for line in res.lines:
                print(f"    {line.text!r} @ {line.rect.as_tuple()} conf={line.confidence}")
        except ImportError:
            print("  cv2.putText unavailable")

    print()
    print("=" * 68)
    print("PROBE COMPLETE")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())