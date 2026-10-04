"""Close a window by posting WM_CLOSE - not by killing its process.

Killing a process to close one of its windows is collateral damage: closing a File Explorer
window that way would take down every other Explorer window the user has open. This posts
a close request to one window only.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "src")

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_CLOSE = 0x0010
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL

from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter


def close_windows(title_contains: str = "", class_name: str = "", process_name: str = "") -> int:
    win = PyWin32WindowAdapter()
    closed = 0
    for w in win.enumerate_windows():
        if not w.is_visible:
            continue
        if title_contains and title_contains.lower() not in w.title.lower():
            continue
        if class_name and w.class_name != class_name:
            continue
        if process_name and w.process_name.lower() != process_name.lower():
            continue
        user32.PostMessageW(wintypes.HWND(w.hwnd), WM_CLOSE, 0, 0)
        closed += 1
    time.sleep(1.0)
    return closed


def main() -> int:
    args = sys.argv[1:]
    title = args[0] if len(args) > 0 else ""
    count = close_windows(title_contains=title)
    print(f"posted WM_CLOSE to {count} window(s) matching title {title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
