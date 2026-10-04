"""Where does OCR time actually go? Breaks the 1.75s figure into components.

Usage: .venv/Scripts/python scripts/probe_ocr_timing.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "src")

from frameforge.adapters.ocr.winrt_ocr import _PS_SCRIPT


def main() -> int:
    import cv2

    canvas = np.zeros((200, 900, 3), np.uint8)
    canvas[:, :] = (12, 12, 16)
    cv2.putText(canvas, "PLAY", (40, 90), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 4)
    cv2.putText(canvas, "SETTINGS", (40, 150), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (230, 230, 230), 3)

    tmp = Path(tempfile.gettempdir())
    script = tmp / "ff_timing.ps1"
    script.write_text(_PS_SCRIPT, encoding="utf-8")
    png = tmp / "ff_timing.png"
    ok, buf = cv2.imencode(".png", cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    png.write_bytes(buf.tobytes())
    out = tmp / "ff_timing.json"

    def run(label: str) -> tuple[float, int]:
        out.unlink(missing_ok=True)
        t0 = time.perf_counter()
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-ImagePath", str(png), "-OutPath", str(out)],
            capture_output=True, timeout=40,
        )
        ms = (time.perf_counter() - t0) * 1000
        lines = 0
        if out.exists():
            try:
                lines = len(json.loads(out.read_text(encoding="utf-8-sig")).get("lines", []))
            except Exception:
                pass
        err = "" if proc.returncode == 0 else (proc.stderr or b"").decode("utf-8", "replace")[:160]
        print(f"  {label:34} {ms:7.0f}ms  rc={proc.returncode} lines={lines} {err}")
        return ms, lines

    print("full OCR pipeline (3 runs):")
    times = []
    for i in range(3):
        ms, lines = run(f"ocr read #{i + 1}")
        times.append(ms)

    print()
    print("component costs:")

    t0 = time.perf_counter()
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
                   capture_output=True, timeout=30)
    print(f"  bare powershell process spawn       {(time.perf_counter() - t0) * 1000:7.0f}ms")

    t0 = time.perf_counter()
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null"],
        capture_output=True, timeout=30)
    print(f"  + Add-Type System.Runtime.WindowsRT  {(time.perf_counter() - t0) * 1000:7.0f}ms")

    print()
    print(f"  mean OCR read: {sum(times) / len(times):.0f}ms")
    print()
    print("Interpretation: a fixed ~600-900ms is process spawn + WinRT type resolution,")
    print("which no amount of caching removes. That is why the assembler treats OCR as an")
    print("expensive, cached, on-demand signal rather than something to run every epoch.")
    for p in (script, png, out):
        p.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())