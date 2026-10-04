"""What do real Windows applications look like to Frame Forge?

Probes each candidate target: launches it, finds its window, and reports the identity and
client geometry Frame Forge would need to write a profile. This is reconnaissance for
authoring profiles against software Frame Forge did not build.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

TARGETS = [
    ("notepad", r"C:\Windows\System32\notepad.exe"),
    ("calc", r"C:\Windows\System32\calc.exe"),
    ("control", r"C:\Windows\System32\control.exe"),
]


def main() -> int:
    win = PyWin32WindowAdapter()
    print("=" * 74)
    print("EXISTING WINDOWS (visible, titled)")
    print("=" * 74)
    for info in win.enumerate_windows():
        if info.is_visible and info.title:
            print(f"  {info.process_name:14} {info.class_name:28} {info.title[:44]!r}")

    for name, exe in TARGETS:
        print()
        print("=" * 74)
        print(f"LAUNCH {name}: {exe}")
        print("=" * 74)
        try:
            proc = subprocess.Popen([exe])
        except Exception as exc:
            print(f"  launch failed: {exc}")
            continue
        time.sleep(3.5)

        windows = [
            w for w in win.enumerate_windows()
            if w.is_visible and w.title and w.pid in (proc.pid, 0)
        ]
        # Apps that re-host themselves report a different pid; match by process name too.
        windows = [
            w for w in win.enumerate_windows()
            if w.is_visible and w.title and w.process_name.lower() == Path(exe).name.lower()
        ]
        if not windows:
            windows = [w for w in win.enumerate_windows() if w.is_visible and w.title][-3:]

        if not windows:
            print("  no window found")
        for w in windows[:6]:
            client = w.client_rect.as_tuple() if w.client_rect else None
            origin = w.client_origin_screen.as_tuple()
            print(f"  pid={w.pid:<6} class={w.class_name!r}")
            print(f"    title={w.title[:60]!r}")
            print(f"    client={client}  origin={origin}  monitor={w.monitor_index}  fg={w.is_foreground}")

        # A profile needs a stable TargetSpec. Report what discriminates this window.
        if windows:
            w = windows[0]
            print()
            print("  suggested TargetSpec:")
            print(f'    {{"title_regex": {w.title.split(chr(32))[0]!r}, '
                  f'"class_name": {w.class_name!r}, '
                  f'"process_name": {w.process_name!r}}}')

        try:
            proc.terminate()
        except Exception:
            pass

    print()
    print("PROBE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())