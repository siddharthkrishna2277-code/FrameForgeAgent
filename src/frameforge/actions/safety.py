"""Input safety: the single authority for synthetic input and machine input state.

This module exists because of a release-blocking defect. Frame Forge used to escalate to
an ALT ``keybd_event`` tap to acquire foreground permission. On a machine with both
``en-US`` and ``en-IN`` installed, that activated the IME and switched the active input
language to ``en-IN``. The state survived every process exit: the user pressed ``N`` and
got an alternate-script character, Backspace and Delete did nothing, and the only recovery
was a Windows restart.

The lesson is not "be careful with Alt". It is that **a keystroke is the only operation
in this system that can change machine-wide state the operator cannot see**. So:

* every synthetic keyboard/mouse event goes through :class:`InputSafetyManager`, and
  nothing else is permitted to call ``SendInput`` or ``keybd_event``;
* every down is paired with an up, and pairing is verified rather than assumed;
* the input language/layout is sampled before a run and restored afterwards if it moved;
* language-switching chord sequences (Alt+Shift, Ctrl+Shift, Win+Space, ...) are refused
  outright unless explicitly allowlisted, because no legitimate Frame Forge action needs
  them;
* cleanup runs on *every* exit path, including cancellation, timeout, estop and unhandled
  exception;
* :func:`recover_input_control` works standalone, so a damaged machine can be repaired
  without a restart.

Audit trail: ``frameforge recover-input --audit`` prints what this module believes it has
touched, and the post-run check writes the same record into the run directory.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# --------------------------------------------------------------------- win32 decls

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x01000
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0004, 0x0008
WHEEL_DELTA = 120
XBUTTON1, XBUTTON2 = 0x0001, 0x0002


#: The one authoritative name -> virtual-key table, imported from the input port so the
#: safety manager and the dispatcher cannot drift apart.
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetKeyboardLayoutList.argtypes = (ctypes.c_int, ctypes.POINTER(wintypes.HKL))
user32.GetKeyboardLayoutList.restype = ctypes.c_int
user32.ActivateKeyboardLayout.argtypes = (wintypes.HKL,)
user32.ActivateKeyboardLayout.restype = wintypes.HKL
user32.GetKeyboardLayout.argtypes = (ctypes.c_int,)
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.LoadKeyboardLayoutW.argtypes = (wintypes.LPCWSTR,)
user32.LoadKeyboardLayoutW.restype = wintypes.HKL
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL

# Modifiers are released unconditionally during cleanup, on both sides, as a defensive
# fallback. If any of these is logically down because a key-up was lost, this is what
# clears it.
#: Codes needing KEYEVENTF_EXTENDEDKEY, duplicated from the input adapter so this module
#: has no import cycle with it (the adapter imports this module).
_EXTENDED_VKS: frozenset[int] = frozenset({
    0xA2, 0xA3, 0xA4, 0xA5, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x5C, 0x5D,
})

MODIFIER_VKS: dict[str, tuple[int, ...]] = {
    "shift": (0x10, 0xA0, 0xA1),
    "ctrl": (0x11, 0xA2, 0xA3),
    "alt": (0x12, 0xA4, 0xA5),
    "win": (0x5B, 0x5C),
    "menu": (0x5D,),
    "space": (0x20,),
    "capslock": (0x14,),
}

#: Defensive release set. Cleanup emits a key-up for every one of these whether or not
#: tracking believes it is down, because a lost key-up leaves state we do not own. This is
#: the list the incident report asked for: both sides of every modifier, plus Enter,
#: Escape, Space, Backspace, Delete, the movement cluster, and the number row.
DEFENSIVE_KEY_VKS: dict[str, int] = {
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3,
    "lalt": 0xA4, "ralt": 0xA5, "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "apps": 0x5D, "f10": 0x79,
    "w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "n0": 0x30, "n1": 0x31, "n2": 0x32, "n3": 0x33, "n4": 0x34,
    "n5": 0x35, "n6": 0x36, "n7": 0x37, "n8": 0x38, "n9": 0x39,
}

BUTTON_VKS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    "x1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
    "x2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
}

#: Left/right variants collapse to their generic name.
#:
#: Load-bearing, and found by a test rather than by reading: the chord table below is
#: written in generic names ("ctrl" + "shift"), while profiles and the action compiler
#: emit the *specific* ones ("lctrl" + "lshift"). Matching the raw strings therefore let
#: a genuine Ctrl+Shift chord through - which is the single most dangerous chord on this
#: machine, because held together every subsequent keypress is interpreted as a layout
#: switch.
_KEY_CANON: dict[str, str] = {
    "lctrl": "ctrl", "rctrl": "ctrl",
    "lshift": "shift", "rshift": "shift",
    "lalt": "alt", "ralt": "alt",
    "lwin": "win", "rwin": "win",
}


def canon_key(name: str) -> str:
    """Canonical form of a key name, for chord matching."""
    lowered = str(name).lower()
    return _KEY_CANON.get(lowered, lowered)


#: Keys Frame Forge refuses to press unless explicitly allowlisted. None of these is
#: needed for any game or UI interaction Frame Forge supports, and each one can change
#: machine-wide input state (guardrail G-ABS-10).
#: F10 is refused outright: bare it opens the "File" menu, and with Shift it opens a
#: context menu. Neither is ever needed to drive an application.
FORBIDDEN_KEYS: frozenset[str] = frozenset({"win", "lwin", "rwin", "menu", "f10"})
#: Keys that participate in an input-language switching chord.
LANGUAGE_SWITCH_KEYS: frozenset[str] = frozenset({"alt", "lalt", "ralt", "shift", "lshift", "rshift", "ctrl", "lctrl", "rctrl", "win", "lwin", "rwin", "space"})


class Violation(StrEnum):
    """Why a dispatch was refused."""

    NONE = "none"
    DISARMED = "disarmed"
    FORBIDDEN_KEY = "forbidden_key"
    LANGUAGE_CHORD = "language_switch_chord"
    NOT_ALLOWLISTED = "not_allowlisted"
    TARGET_INVALID = "target_invalid"
    TARGET_DENIED = "target_denied"
    TARGET_NOT_FOREGROUND = "target_not_foreground"
    RIGHT_CLICK_NOT_ALLOWED = "right_click_not_allowed"
    MENU_KEY = "menu_key"


# --------------------------------------------------------------------------- deny-list
#
# Windows cannot distinguish injected input from a human hand: injected events are
# serialised into the same stream. So the only real protection is *not sending them*, and
# that means refusing to target anything that is not a test surface.
#
# This was learned the hard way. A hardware test sent a right-button down/up at whatever
# was under the cursor; the foreground window was the Frame Forge agent's own UI, and the
# user watched context menus open by themselves. Injected input has no target - it lands
# on whatever is foreground, which is why targeting must be validated rather than assumed.

#: Window classes never acceptable as an automation target. Compared lowercased, so these
#: are stored lowercase.
DENIED_CLASSES: frozenset[str] = frozenset({
    "progman",                  # the desktop shell
    "shell_traywnd",            # the taskbar
    "consolewindowclass",       # a console window
    "codetoplevel",             # editors and IDEs
    "chrome_widgetwin_1",       # browsers and Electron apps, incl. the agent's own UI
    "applicationframewindow",
    "windows.ui.core.corewindow",   # UWP host surfaces
    "#32770",                   # dialogs: a context menu is one of these
    "notifyiconoverflowwindow",
    "tooltips_class32",
    # XAML/WinUI popups. A stray Menu key or context-menu trigger in a modern Windows app
    # materialises one of these *owned by the target*, and it sits on top of the very
    # pixels the run is trying to perceive. Observed against Notepad: an Escape that
    # activated the menu bar created `Microsoft.UI.Content.PopupWindowSiteBridge`, after
    # which every landmark read as absent - the run was reading the popup, not Notepad.
    "microsoft.ui.content.popupwindowbridge",
    "microsoft.ui.content.popupwindow",
    "popuphost",
    "xaml_desktop_window_content",
    "xaml_windowedpopup_class",
    "windowedpopups_class",
    "expreviewhoverwindow",
    # vguiPopupWindow is what Notepad creates the moment its menu bar is activated -
    # including by an Escape, because Escape activates and then closes the menu. It sits
    # over the very pixels a run is trying to read. Measured: after one Escape keypress,
    # every landmark scored 0.000 because perception was reading the popup.
    "vguipopupwindow",
    "xaml_menu",
    "netuiowindow",
    "popup",
    "menupopup",
})

#: Popup classes that are the *target's own* transient UI rather than a foreign occluder.
#: Their presence is a signal to report, not a reason to declare the target covered.
POPUP_CLASSES: frozenset[str] = frozenset({
    "vguipopupwindow",
    "microsoft.ui.content.popupwindowbridge",
    "microsoft.ui.content.popupwindow",
    "popuphost",
    "xaml_desktop_window_content",
    "xaml_windowedpopup_class",
    "windowedpopups_class",
})

#: Process names never acceptable as an automation target. Lowercase; compared lowercased.
DENIED_PROCESSES: frozenset[str] = frozenset({
    # shell and desktop
    "explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "searchapp.exe", "textinputhost.exe", "ctfmon.exe",
    # terminals and editors
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "code.exe", "code - insiders.exe", "pycharm64.exe", "devenv.exe", "notepad++.exe",
    "devenv", "pycharm", "code-oss.exe", "atom.exe", "notepad2.exe",
    # browsers and electron apps, including the agent's own UI
    "chrome.exe", "msedge.exe", "firefox.exe", "hermes.exe", "hermes-agent.exe",
    "electron.exe", "slack.exe", "discord.exe", "teams.exe", "zoom.exe",
    # settings, credentials, system
    "systemsettings.exe", "control.exe", "consent.exe", "credentialuibroker.exe",
    "credentialui.exe", "logonui.exe", "lockapp.exe", "lsass.exe", "winlogon.exe",
    # audio: FxSound and friends own global hotkeys we must never reach
    "audiodg.exe", "fxsound.exe", "rtkvrcop.exe", "realtek.exe",
})

#: Title fragments that mark a protected window (chat, terminals, prompts).
DENIED_TITLE_FRAGMENTS: tuple[str, ...] = (
    "password", "credential", "sign in", "logon", "secure desktop",
    "windows security", "user account control", "confirm your identity",
)

#: Keys that open a context menu without a mouse. Shift+F10 is the keyboard route to the
#: same thing a right-click produces, and VK_APPS / VK_MENU is the dedicated key for it.
VK_APPS, VK_MENU_KEY = 0x5D, 0x5D
MENU_TRIGGER_KEYS: frozenset[str] = frozenset({
    "menu", "apps", "app", "contextmenu", "context_menu", "shift+f10",
})


@dataclass(frozen=True, slots=True)
class TargetWindow:
    """Identity of the window an action is allowed to touch."""

    hwnd: int
    title: str = ""
    class_name: str = ""
    process_name: str = ""
    pid: int = 0
    is_foreground: bool = False

    def describe(self) -> str:
        return (f"{self.process_name}[{self.pid}] {self.class_name!r} {self.title[:40]!r} "
                f"hwnd={self.hwnd}")

    def to_dict(self) -> dict[str, object]:
        return {"hwnd": self.hwnd, "title": self.title[:80], "class_name": self.class_name,
                "process_name": self.process_name, "pid": self.pid,
                "is_foreground": self.is_foreground}


#: pid -> process name, cached.
#:
#: The first version of this resolved the process name with a ``tasklist`` subprocess.
#: Measured: **205 ms per call**. That single line put a 183 ms stall on every input
#: dispatch and on every cleanup, which is what pushed emergency-stop latency from 40 ms
#: to over 600 ms. Process names are cached and resolved by PID only once.
_PROC_CACHE: dict[int, str] = {}
_PROC_CACHE_BUSTED: float = 0.0


def _process_name_for(pid: int, *, max_age_s: float = 30.0) -> str:
    """Resolve a pid to its **full executable path**, cached.

    Returns the absolute image path, not a bare name: identity verification compares the path
    against the filesystem and against the reported process name, and a basename alone can
    neither be checked for existence nor distinguished from a same-named program elsewhere
    on the PATH.

    This replaces an earlier implementation that shelled out to ``tasklist`` - measured at
    **205 ms per call**, which put that cost on every dispatch and every cleanup and
    inflated emergency-stop latency from 16 ms to 643 ms.

    Cached because it is on the hot path and a process's image does not change while it
    runs; busted after ``max_age_s`` so a recycled pid cannot be mistaken for the original.
    """
    global _PROC_CACHE_BUSTED
    now = time.monotonic()
    if now - _PROC_CACHE_BUSTED > max_age_s:
        _PROC_CACHE.clear()
        _PROC_CACHE_BUSTED = now
    if pid in _PROC_CACHE:
        return _PROC_CACHE[pid]

    name = ""
    try:
        import ctypes as _c

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = _c.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, _c.POINTER(wintypes.DWORD)
        )
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buf = _c.create_unicode_buffer(size.value)
                if k32.QueryFullProcessImageNameW(handle, 0, buf, _c.byref(size)):
                    name = buf.value
            finally:
                k32.CloseHandle(handle)
    except Exception:
        name = ""
    _PROC_CACHE[pid] = name
    return name


#: The two accessors differ in one way that matters, and conflating them broke identity
#: verification: a *display name* is a label used for deny-list matching, while an *image
#: path* is an identity that must exist on disk and agree with the label.
process_image_path_for = _process_name_for


def process_display_name(pid: int) -> str:
    """Basename of a process image, for display and deny-list matching."""
    import os as _os

    return _os.path.basename(_process_name_for(pid))


def _deny_list_is_normalised() -> bool:
    """Deny-list entries must be lowercase and unstripped, because comparison lowercases.

    A stray trailing space makes an entry silently unmatchable - which is how "Code.exe"
    briefly slipped through the editor ban. Asserted by the test suite so it cannot recur.
    """
    return all(e == e.lower() and e == e.strip()
               for e in (*DENIED_CLASSES, *DENIED_PROCESSES))


def read_foreground_window() -> TargetWindow | None:
    """Identify the foreground window. Read-only; never changes focus."""
    try:
        hwnd = int(user32.GetForegroundWindow() or 0)
    except Exception:
        return None
    if not hwnd:
        return None
    try:
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        buf = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, len(buf))
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), cbuf, 256)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return TargetWindow(
            hwnd=hwnd, title=buf.value, class_name=cbuf.value,
            process_name=process_display_name(int(pid.value)),
            pid=int(pid.value), is_foreground=True,
        )
    except Exception:
        return None


def classify_target(target: TargetWindow | None) -> tuple[bool, str]:
    """Is this window acceptable as an automation target? Returns ``(ok, reason)``."""
    if target is None:
        return False, "no foreground window"
    cls = (target.class_name or "").lower()
    proc = (target.process_name or "").lower()
    title = (target.title or "").lower()
    if cls in DENIED_CLASSES:
        return False, f"window class {target.class_name!r} is deny-listed"
    if proc in DENIED_PROCESSES:
        return False, f"process {target.process_name!r} is deny-listed"
    if cls == "#32770" and not title.strip():
        return False, "an unnamed dialog (#32770) is deny-listed"
    for fragment in DENIED_TITLE_FRAGMENTS:
        if fragment in title:
            return False, f"window title matches protected pattern {fragment!r}"
    return True, ""


@dataclass(frozen=True, slots=True)
class LayoutState:
    """The machine's input language at a point in time."""

    hkl: int
    locale: int
    layout: int

    @property
    def locale_hex(self) -> str:
        return f"0x{self.locale:04X}"

    def describe(self) -> str:
        return f"HKL=0x{self.hkl:08X} locale={self.locale_hex} layout=0x{self.layout:04X}"

    def same_language(self, other: LayoutState | None) -> bool:
        return other is not None and (other.locale, other.layout) == (self.locale, self.layout)

    def to_dict(self) -> dict[str, object]:
        return {"hkl": f"0x{self.hkl:08X}", "locale": self.locale_hex,
                "layout": f"0x{self.layout:04X}"}


SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77


def virtual_size() -> tuple[int, int]:
    return (
        max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def cursor_position():
    from frameforge.ports.geometry import Point

    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return Point(int(pt.x), int(pt.y))
    return Point(0, 0)


def read_layout() -> LayoutState:
    """Read the active keyboard layout without changing anything."""
    hkl = int(user32.GetKeyboardLayout(0) or 0)
    return LayoutState(hkl=hkl, locale=hkl & 0xFFFF, layout=(hkl >> 16) & 0xFFFF)


def list_layouts() -> list[int]:
    n = int(user32.GetKeyboardLayoutList(0, None) or 0)
    if n <= 0:
        return []
    arr = (wintypes.HKL * n)()
    user32.GetKeyboardLayoutList(n, arr)
    return [int(h) for h in arr]


WM_INPUTLANGCHANGEREQUEST = 0x0010
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = ()


def set_layout(hkl: int, *, timeout_s: float = 0.6) -> dict[str, object]:
    """Change the active input language *persistently*.

    ``ActivateKeyboardLayout`` alone is not enough: it sets the layout for the **calling
    thread only**, so the change vanishes the moment the process exits. That was measured -
    recovery appeared to succeed and the layout silently reverted on exit.

    A persistent change needs both halves:

    * ``ActivateKeyboardLayout`` so this thread is immediately correct, and
    * ``WM_INPUTLANGCHANGEREQUEST`` posted to the foreground window, which is the message
      Windows uses to change an application's input language.

    Both are attempted; the result says which took effect. No keystroke is involved.
    """
    before = read_layout()
    result: dict[str, object] = {
        "before": before.describe(),
        "requested": f"0x{hkl:08X}",
        "activate_thread": False,
        "post_message": False,
    }
    if hkl <= 0:
        result["error"] = "invalid HKL"
        return result

    try:
        user32.ActivateKeyboardLayout(wintypes.HKL(hkl))
        result["activate_thread"] = True
    except Exception as exc:
        result["activate_error"] = str(exc)

    try:
        fg = user32.GetForegroundWindow()
        if fg:
            user32.PostMessageW(wintypes.HWND(fg), WM_INPUTLANGCHANGEREQUEST, 0, hkl)
            result["post_message"] = True
            result["foreground_hwnd"] = int(fg)
    except Exception as exc:
        result["post_error"] = str(exc)

    time.sleep(timeout_s)
    after = read_layout()
    result["after"] = after.describe()
    result["applied"] = after.hkl == hkl
    return result


def is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def stuck_modifiers() -> dict[str, bool]:
    """Which modifiers Windows currently believes are down."""
    out: dict[str, bool] = {}
    for name, vks in MODIFIER_VKS.items():
        out[name] = any(is_key_down(vk) for vk in vks)
    return out


@dataclass
class HeldItem:
    """A key or button the controller believes is down, with ownership and a deadline."""

    name: str
    kind: str                 # "key" | "button" | "vk"
    run_id: str = ""
    action_id: str = ""
    thread_id: int = 0
    pid: int = 0
    pressed_mono_ms: float = 0.0
    max_hold_ms: float = 2000.0

    def overdue(self, now_ms: float) -> bool:
        return (now_ms - self.pressed_mono_ms) > self.max_hold_ms

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "run_id": self.run_id,
                "action_id": self.action_id, "thread_id": self.thread_id, "pid": self.pid,
                "held_ms": round(self.max(0.0, 0.0), 1)}


@dataclass
class DispatchRecord:
    """Audit entry for one synthetic event batch.

    Carries the fields the incident report requires: sequence, run/action id, source,
    thread/process, event type, target and foreground window, the pressed registry before
    and after, and the outcome.
    """

    mono_ms: float
    kind: str
    count: int
    event_id: int = 0
    run_id: str = ""
    action_id: str = ""
    source: str = ""
    thread_id: int = 0
    pid: int = 0
    target: dict[str, object] = field(default_factory=dict)
    foreground: dict[str, object] = field(default_factory=dict)
    pressed_before: tuple[str, ...] = ()
    pressed_after: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()
    violation: str = Violation.NONE
    detail: str = ""
    outcome: str = ""


class InputSafetyManager:
    """The only permitted path to synthetic input, and the owner of input-state hygiene.

    Responsibilities:

    * refuse language-switch chords and the Windows key outright;
    * guarantee that every down it emits is matched by an up, on every exit path;
    * sample the input layout before a run and restore it afterwards if it moved;
    * track every worker/hook/child it owns so cleanup can stop them;
    * record everything it did, so a post-run audit can prove it left nothing behind.
    """

    #: Chord pairs that switch input language or layout. Refused as a unit.
    LANGUAGE_CHORDS: frozenset[frozenset[str]] = frozenset({
        frozenset({"alt", "shift"}),
        frozenset({"ctrl", "shift"}),
        frozenset({"win", "space"}),
        frozenset({"ctrl", "space"}),
        frozenset({"alt", "space"}),
    })

    #: Maximum time any key or button may be held before the watchdog force-releases it.
    #: A hold with no bound is how a stuck modifier happens in the first place.
    DEFAULT_MAX_HOLD_MS = 2000.0

    def __init__(
        self,
        *,
        allow_language_switch: bool = False,
        allow_forbidden_keys: bool = False,
        clock=None,
        max_records: int = 5000,
        run_id: str = "",
        #: The window actions are permitted to touch. ``None`` means "no target
        #: validation", which is only ever acceptable in a unit test with a mocked backend.
        target_hwnd: int | None = None,
        require_target_foreground: bool = True,
        allow_right_click: bool = False,
        max_hold_ms: float = DEFAULT_MAX_HOLD_MS,
        verbose_events: bool = False,
    ) -> None:
        self._clock = clock
        self.allow_language_switch = allow_language_switch
        self.allow_forbidden_keys = allow_forbidden_keys
        self.enabled = False
        self.run_id = run_id
        #: Authoritative pressed-state registry, with ownership and deadlines.
        self._held: dict[str, HeldItem] = {}
        self._held_vks: set[int] = set()
        self._records: list[DispatchRecord] = []
        self._max_records = max_records
        self._seq = 0
        self.initial_layout: LayoutState | None = None
        self.final_layout: LayoutState | None = None
        self._closables: list[tuple[str, object]] = []
        self.cleanups = 0
        self.violations = 0
        #: Presses refused because the manager was disarmed. Surfaced in the audit summary
        #: so a silently dropped action cannot hide behind an otherwise passing report.
        self.blocked_presses = 0
        self.last_violation_detail = ""
        #: Target validation.
        self.target_hwnd = target_hwnd
        self.require_target_foreground = require_target_foreground
        self.allow_right_click = allow_right_click
        self.max_hold_ms = max_hold_ms
        self.verbose_events = verbose_events
        #: Set when cleanup could not be verified. The run must not report success then.
        self.cleanup_ok = True
        self.watchdog_releases: list[str] = []
        self._action_seq = 0

    # ------------------------------------------------------------- compatibility

    @property
    def held(self) -> tuple[str, ...]:
        """Everything currently believed down. The authoritative registry, as a tuple."""
        return tuple(sorted(self._held))

    @property
    def held_detail(self) -> tuple[HeldItem, ...]:
        return tuple(self._held[n] for n in sorted(self._held))

    @property
    def _held_keys(self) -> set[str]:
        return {k for k, v in self._held.items() if v.kind == "key"}

    @property
    def _held_buttons(self) -> set[str]:
        return {k for k, v in self._held.items() if v.kind == "button"}

    def next_action_id(self) -> str:
        self._action_seq += 1
        return f"a{self._action_seq:05d}"

    # ------------------------------------------------------------------- watchdog

    def enforce_deadlines(self) -> list[str]:
        """Force-release anything held past its deadline.

        Returns the names released. Called before every dispatch and during cleanup, so a
        hold cannot outlive its bound even if the caller simply stops.
        """
        now = self._now_ms()
        released: list[str] = []
        for name, item in list(self._held.items()):
            if item.overdue(now):
                try:
                    if item.kind == "button":
                        self._button(item.name, down=False)
                    else:
                        self._key(item.name, up=True)
                    released.append(name)
                except Exception:
                    released.append(name)
                self._held.pop(name, None)
        self.watchdog_releases.extend(released)
        if released:
            self._record("watchdog", len(released), detail=f"force-released {released}")
        return released

    def _now_ms(self) -> float:
        return (self._clock.monotonic_ms() if self._clock
                else time.perf_counter() * 1000.0)

    # ------------------------------------------------------------- target checks

    def validate_target(self) -> tuple[bool, str]:
        """Refuse input unless the permitted window is the foreground window.

        Injected input has no target: it lands on whatever is foreground. So the check is
        not "is the window on screen" but "is the window I was told to drive actually the
        one in front right now".
        """
        if self.target_hwnd is None:
            return True, ""          # no target configured (mock/test backends only)
        fg = read_foreground_window()
        if fg is None:
            return False, "no foreground window"
        ok, reason = classify_target(fg)
        if not ok:
            return False, f"foreground window is deny-listed: {reason}"
        if fg.hwnd != self.target_hwnd:
            return False, (
                f"foreground {fg.describe()} is not the target "
                f"(hwnd={self.target_hwnd})"
            )
        return True, ""

    # ------------------------------------------------------------------ lifecycle

    def begin_run(self) -> LayoutState:
        """Sample the input layout before anything is sent."""
        self.initial_layout = read_layout()
        self._held_keys.clear()
        self._held_buttons.clear()
        self._held_vks.clear()
        self._records.clear()
        self.cleanups = 0
        self.violations = 0
        return self.initial_layout

    def register_closable(self, name: str, handle: object) -> None:
        """Register a hook, worker, or child process that cleanup must stop."""
        self._closables.append((name, handle))

    # --------------------------------------------------------------------- checks

    def check_chord(self, keys: Iterable[str]) -> tuple[Violation, str]:
        """Refuse anything that could change machine-wide input state."""
        # Canonicalise left/right variants before matching, or a real Ctrl+Shift chord
        # sent as "lctrl"+"lshift" would not match the generic table entries.
        lowered = {canon_key(k) for k in keys}
        if not self.allow_forbidden_keys:
            bad = lowered & FORBIDDEN_KEYS
            if bad:
                return Violation.FORBIDDEN_KEY, f"refused key(s) {sorted(bad)}"
        if not self.allow_language_switch:
            for chord in self.LANGUAGE_CHORDS:
                if chord.issubset(lowered):
                    return (
                        Violation.LANGUAGE_CHORD,
                        f"refused input-language switching chord {sorted(chord)}",
                    )
        return Violation.NONE, ""

    # -------------------------------------------------------------------- dispatch

    def send(self, primitive, *, action_id: str = "", source: str = "") -> bool:
        """Dispatch one primitive through the audited path. Returns whether it went out.

        Order of checks matters and is deliberate:

        1. watchdog - release anything already overdue;
        2. menu-key / chord guards - refuse anything that could change machine state;
        3. right-click gate - off unless explicitly allowed;
        4. target validation - injected input has no target, so the foreground window must
           be the one we were told to drive;
        5. arm gate - a *press* needs the port armed; a *release* never does.

        A release is dispatched even when disarmed. Blocking a release is exactly what
        stranded Ctrl and Alt on the operator's machine.
        """
        from frameforge.adapters.input.sendinput import is_release

        self.enforce_deadlines()
        releasing = is_release(primitive)

        violation, detail = self._screen(primitive)
        if violation is not Violation.NONE:
            self.violations += 1
            self.last_violation_detail = detail
            self._record(primitive.kind.value, 0, violation=str(violation), detail=detail,
                         action_id=action_id, source=source)
            return False

        if not self.enabled and not releasing:
            # Counted as a violation, not only recorded per-event. A refused press that is
            # invisible in the summary is how the first live run reported "7 pass / 2 fail"
            # while silently dropping half its primitives.
            self.violations += 1
            self.blocked_presses += 1
            self.last_violation_detail = "input disarmed"
            self._record(primitive.kind.value, 0, violation=str(Violation.DISARMED),
                         detail="input disarmed", action_id=action_id, source=source)
            return False

        ok, why = self.validate_target()
        if not ok:
            self.violations += 1
            self.last_violation_detail = why
            self._record(primitive.kind.value, 0, violation=str(Violation.TARGET_INVALID),
                         detail=why, action_id=action_id, source=source)
            return False

        self._inject(primitive)
        self._track(primitive, action_id=action_id)
        self._record(primitive.kind.value, 1, action_id=action_id, source=source)
        return True

    def _screen(self, primitive) -> tuple[Violation, str]:
        """Refuse the events that produce the observed symptoms."""
        from frameforge.ports.input import PrimitiveType

        keys = _keys_of(primitive)
        lowered = {str(k).lower() for k in keys}

        # VK_APPS / the Menu key opens a context menu with no mouse involved, which is
        # indistinguishable from a user action once it reaches a foreground window.
        if lowered & MENU_TRIGGER_KEYS:
            return Violation.MENU_KEY, (
                f"refused context-menu key {sorted(lowered & {'menu', 'apps'})}: "
                "VK_APPS/Menu produces a context menu and is never required"
            )
        # Shift+F10 is the keyboard route to the same context menu, and either half can be
        # pressed first, so both the chord and the bare F10 are refused.
        if "f10" in lowered:
            return Violation.MENU_KEY, "refused F10 (context-menu key)"
        if "shift" in lowered and "f10" in lowered:
            return Violation.MENU_KEY, "refused Shift+F10 (context-menu chord)"

        if primitive.kind is PrimitiveType.MOUSE_BUTTON:
            button = str(getattr(primitive, "button", ""))
            if button == "right" and primitive.down and not self.allow_right_click:
                return Violation.RIGHT_CLICK_NOT_ALLOWED, (
                    "right-click is disabled by default; it must be enabled explicitly for "
                    "a declared scenario step (see G-ABS-11)"
                )

        return self.check_chord(keys)

    def send_raw_vk(self, vk: int, *, up: bool, armed: bool = True) -> bool:
        """Press or release a raw virtual-key code, tracked like any other key.

        Replaces an earlier untracked escape hatch which pressed keys the port could not
        release and refused to release once disarmed - the direct cause of stuck modifiers
        after an emergency stop. Raw codes are still needed to self-test the global-hotkey
        hook; they now go through the same accounting as everything else.
        """
        name = f"vk:0x{vk:02X}"
        if not armed and not up:
            self._record("raw_vk", 0, violation=str(Violation.DISARMED), detail=name)
            return False
        item = _INPUT(type=INPUT_KEYBOARD)
        flags = KEYEVENTF_KEYUP if up else 0
        if vk in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        item.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
        self._submit(item)
        if up:
            self._held_vks.discard(vk)
        else:
            self._held_vks.add(vk)
        self._record("raw_vk", 1, detail=name)
        return True

    def send_batch(self, primitives: list) -> int:
        return sum(1 for p in primitives if self.send(p))

    def _record(self, kind: str, count: int, violation: str = Violation.NONE,
                detail: str = "", *, action_id: str = "", source: str = "") -> None:
        """Append an event-level audit record, with the registry either side of the event."""
        if len(self._records) >= self._max_records:
            return
        import os
        import threading

        mono = self._now_ms()
        before = tuple(sorted(self._held))
        self._seq += 1
        fg = read_foreground_window() if self.verbose_events or True else None
        record = DispatchRecord(
            mono_ms=round(mono, 3),
            event_id=self._seq,
            run_id=self.run_id,
            action_id=action_id,
            source=source or _callsite(),
            thread_id=threading.get_ident(),
            pid=os.getpid(),
            kind=kind,
            count=count,
            target={"hwnd": self.target_hwnd} if self.target_hwnd else {},
            foreground=fg.to_dict() if fg else {},
            pressed_before=before,
            pressed_after=tuple(sorted(self._held)),
            violation=str(violation),
            detail=detail[:300],
            outcome="blocked" if violation != Violation.NONE else "delivered",
        )
        self._records.append(record)
        if self.verbose_events:
            print(
                f"[input] {record.event_id:06d} {kind} n={count} {record.outcome} "
                f"before={list(before)} after={list(record.pressed_after)} "
                f"fg={record.foreground.get('process_name','')} "
                f"callsite={record.source}",
                flush=True,
            )

    def _track(self, primitive, *, action_id: str = "") -> None:
        from frameforge.ports.input import PrimitiveType

        import os
        import threading

        now = self._now_ms()
        common = {
            "run_id": self.run_id,
            "action_id": action_id or self.next_action_id(),
            "thread_id": threading.get_ident(),
            "pid": os.getpid(),
            "pressed_mono_ms": now,
            "max_hold_ms": self.max_hold_ms,
        }
        if primitive.kind is PrimitiveType.KEY and primitive.key is not None:
            name = str(primitive.key)
            if primitive.down:
                self._held[name] = HeldItem(name=name, kind="key", **common)
            else:
                self._held.pop(name, None)
        elif primitive.kind is PrimitiveType.MOUSE_BUTTON and primitive.button is not None:
            name = str(primitive.button)
            if primitive.down:
                self._held[name] = HeldItem(name=name, kind="button", **common)
            else:
                self._held.pop(name, None)

    # ------------------------------------------------------------------- injection

    def _inject(self, primitive) -> None:
        from frameforge.ports.input import PrimitiveType

        kind = primitive.kind
        if kind is PrimitiveType.MOUSE_MOVE_ABS:
            # MOUSEEVENTF_ABSOLUTE expects normalised 0..65535 units across the *whole*
            # virtual desktop (VIRTUALDESK), not pixels. Passing raw pixels pins the
            # cursor near the top-left corner - which is exactly what happened when this
            # normalisation was lost in a refactor.
            nx, ny = self._abs_norm(primitive.x, primitive.y)
            self._mouse(nx, ny, MOUSEEVENTF_MOVE
                        | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
        elif kind is PrimitiveType.MOUSE_MOVE_REL:
            self._mouse(primitive.dx, primitive.dy, MOUSEEVENTF_MOVE)
        elif kind is PrimitiveType.MOUSE_BUTTON:
            self._button(str(primitive.button), bool(primitive.down))
        elif kind is PrimitiveType.SCROLL:
            if primitive.scroll_y:
                self._mouse(0, 0, MOUSEEVENTF_WHEEL, int(primitive.scroll_y * WHEEL_DELTA))
            if primitive.scroll_x:
                self._mouse(0, 0, MOUSEEVENTF_HWHEEL, int(primitive.scroll_x * WHEEL_DELTA))
        elif kind is PrimitiveType.KEY:
            self._key(str(primitive.key), up=not primitive.down)
        elif kind is PrimitiveType.SCANCODE:
            self._scancode(int(primitive.scancode), up=not primitive.down)
        elif kind is PrimitiveType.UNICODE:
            for ch in primitive.text:
                self._unicode_char(ch)

    def _abs_norm(self, x: int, y: int) -> tuple[int, int]:
        """Virtual-desktop pixels -> normalised 0..65535."""
        vw, vh = virtual_size()
        nx = int(x * 65535 / max(1, vw - 1))
        ny = int(y * 65535 / max(1, vh - 1))
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def _mouse(self, dx: int, dy: int, flags: int, data: int = 0) -> None:
        item = _INPUT(type=INPUT_MOUSE)
        item.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0,
                              dwExtraInfo=None)
        self._submit(item)

    def _button(self, name: str, down: bool) -> None:
        flags = BUTTON_VKS.get(name)
        if flags is None:
            return
        data = 0
        if name in ("x1", "x2"):
            data = XBUTTON1 if name == "x1" else XBUTTON2
        self._mouse(0, 0, flags[0] if down else flags[1], data)

    def _key(self, name: str, *, up: bool) -> None:
        vk = _vk_for(name)
        if vk is None:
            return
        flags = KEYEVENTF_KEYUP if up else 0
        item = _INPUT(type=INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
        self._submit(item)

    def _unicode_char(self, ch: str) -> None:
        """Emit one character as a unicode down/up pair.

        The key-up is not optional. A KEYEVENTF_UNICODE *press alone* leaves the character
        in flight: an edit control that pairs WM_KEYDOWN/WM_CHAR with WM_KEYUP - which
        Notepad's does - never commits it. The original implementation sent the down event
        only, and three consecutive live runs each delivered 12 characters to a correctly
        identified, correctly focused Notepad with 0 violations and produced no text at all.

        The pair is submitted together so the two events cannot be separated by another
        thread's input.
        """
        pair = (_INPUT * 2)()
        pair[0] = _INPUT(type=INPUT_KEYBOARD)
        pair[0].ki = _KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE, time=0,
                                 dwExtraInfo=None)
        pair[1] = _INPUT(type=INPUT_KEYBOARD)
        pair[1].ki = _KEYBDINPUT(wVk=0, wScan=ord(ch),
                                 dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0,
                                 dwExtraInfo=None)
        self._submit_many(pair, 2)

    def _scancode(self, scancode: int, *, up: bool) -> None:
        """Emit a real set-1 hardware scancode.

        Requires ``wVk=0`` plus ``KEYEVENTF_SCANCODE``. Passing a virtual key alongside
        the scancode makes Windows ignore the scancode, which is precisely the silent
        no-op this path replaces.
        """
        if scancode <= 0:
            return
        from frameforge.adapters.input.sendinput import _SC_EXTENDED

        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        if scancode in _SC_EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        item = _INPUT(type=INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(wVk=0, wScan=scancode, dwFlags=flags, time=0,
                              dwExtraInfo=None)
        self._submit(item)

    def _submit_many(self, array, count: int) -> None:
        """Submit an ``_INPUT`` array as one atomic SendInput call.

        The array must be materialised as ``_INPUT * n`` *and* passed byref as that same
        type. Passing a plain list, or a pointer of a different type, raises
        ``expected LP__INPUT instance`` - which is what happened the first time.
        """
        if count == 0:
            return
        # SendInput.argtypes pins arg 2 to POINTER(_INPUT), which an array pointer does not
        # satisfy - ctypes raises "expected LP__INPUT instance instead of pointer to
        # _INPUT_Array_2". The cast is explicit and safe: the array really is contiguous
        # _INPUT structs, which is exactly what the declaration demands.
        ptr = ctypes.cast(array, ctypes.POINTER(_INPUT))
        sent = user32.SendInput(count, ptr, ctypes.sizeof(_INPUT))
        if sent != count:
            raise OSError(ctypes.get_last_error(),
                          f"SendInput delivered {sent} of {count} (target may be elevated)")

    def _submit(self, item) -> None:
        sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
        if sent != 1:
            raise OSError(ctypes.get_last_error(), "SendInput failed (target may be elevated)")

    # --------------------------------------------------------------------- cleanup

    def arm(self) -> None:
        self.enabled = True

    def disarm(self) -> None:
        self.enabled = False

    def release_all(self, *, thorough: bool = True) -> dict[str, object]:
        """Release everything, defensively. Safe to call repeatedly and while disarmed.

        Order matters: tracked holds first (so we know what we owe), then a *defensive*
        sweep of every modifier both sides, the context-menu key, and the full
        movement/number cluster, then all mouse buttons. The defensive part is
        unconditional: a lost key-up leaves state this manager does not believe it owns,
        and a stuck Shift plus an F10 is a context menu appearing in somebody's window.
        """
        report: dict[str, object] = {"keys_released": [], "buttons_released": [],
                                    "mods_swept": [], "defensive_keys": [],
                                    "errors": []}

        for name in sorted(self._held):
            try:
                self._key(name, up=True)
                report["keys_released"].append(name)
            except Exception as exc:
                report["errors"].append(f"key {name}: {exc}")
        self._held.clear()

        for vk in sorted(self._held_vks):
            try:
                item = _INPUT(type=INPUT_KEYBOARD)
                flags = KEYEVENTF_KEYUP
                if vk in _EXTENDED_VKS:
                    flags |= KEYEVENTF_EXTENDEDKEY
                item.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
                self._submit(item)
                report["keys_released"].append(f"vk:0x{vk:02X}")
            except Exception as exc:
                report["errors"].append(f"vk 0x{vk:02X}: {exc}")
        self._held_vks.clear()

        for name in sorted(self._held):
            item = self._held[name] if name in self._held else None
            try:
                if item is None or item.kind == "button":
                    self._button(name, down=False)
                    report["buttons_released"].append(name)
            except Exception as exc:
                report["errors"].append(f"button {name}: {exc}")
        self._held.clear()

        # One batch for every modifier, both sides.
        mod_items: list[tuple[str, int]] = []
        for mod, vks in MODIFIER_VKS.items():
            for vk in vks:
                mod_items.append((f"{mod}:0x{vk:02X}", vk))
        if mod_items:
            items = (_INPUT * len(mod_items))()
            for slot, (label, vk) in enumerate(mod_items):
                items[slot].type = INPUT_KEYBOARD
                items[slot].ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP,
                                             time=0, dwExtraInfo=None)
            try:
                sent = user32.SendInput(len(mod_items), items, ctypes.sizeof(_INPUT))
                report["mods_swept"] = [label for label, _ in mod_items]
                if sent != len(mod_items):
                    report["errors"].append(
                        f"modifier sweep: only {sent}/{len(mod_items)} delivered"
                    )
            except Exception as exc:
                report["errors"].append(f"modifier sweep: {exc}")

        # Every mouse button, every time, whatever tracking believes.
        for name in ("left", "right", "middle", "x1", "x2"):
            try:
                self._button(name, down=False)
                if name not in report["buttons_released"]:
                    report["buttons_released"].append(name)
            except Exception as exc:
                report["errors"].append(f"button {name}: {exc}")

        # Defensive sweep of the keys most likely to have been stranded, including the
        # context-menu key and F10.
        #
        # Sent as ONE SendInput batch rather than ~35 individual calls. Measured: the
        # per-call version pushed emergency-stop latency from 40 ms to 700 ms, which breaks
        # the sub-100 ms requirement - a slow emergency stop is a real hazard, and the
        # sweep is worth having so it has to be fast rather than merely thorough.
        defensive = ([(name, vk) for name, vk in DEFENSIVE_KEY_VKS.items()]
                     if thorough else [])
        if defensive:
            items = (_INPUT * len(defensive))()
            for slot, (name, vk) in enumerate(defensive):
                flags = KEYEVENTF_KEYUP
                if vk in _EXTENDED_VKS:
                    flags |= KEYEVENTF_EXTENDEDKEY
                items[slot].type = INPUT_KEYBOARD
                items[slot].ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                                             dwExtraInfo=None)
            try:
                sent = user32.SendInput(len(defensive), items, ctypes.sizeof(_INPUT))
                if sent == len(defensive):
                    report["defensive_keys"] = [n for n, _ in defensive]
                else:
                    report["errors"].append(
                        f"defensive sweep: only {sent}/{len(defensive)} key-ups delivered"
                    )
            except Exception as exc:
                report["errors"].append(f"defensive sweep: {exc}")

        # Stop every registered hook/worker/child.
        stopped: list[str] = []
        for name, handle in list(self._closables):
            try:
                closer = getattr(handle, "uninstall_hotkey", None) or getattr(handle, "close", None)
                if callable(closer):
                    closer()
                stop = getattr(handle, "stop", None)
                if callable(stop):
                    stop()
                join = getattr(handle, "join", None)
                if callable(join):
                    join(timeout=2.0)
                stopped.append(name)
            except Exception as exc:
                report["errors"].append(f"closable {name}: {exc}")
        report["closables_stopped"] = stopped
        self._closables.clear()

        self.cleanups += 1
        # Deliberately does NOT disarm.
        #
        # A release sweep is a reconciliation of what is physically held, not a revocation
        # of press authority. The previous code ended here with `self.enabled = False`, and
        # because the executor releases on every batch exit while the controller releases
        # after every dispatched primitive, the *first* action of a run disarmed the manager
        # and every later press was refused with "input disarmed". The first live POC run
        # lost its typed text to exactly this, invisibly: releases are supposed to bypass
        # the arm gate, so the sweep looked like it was working.
        #
        # Stopping input is a separate, explicit act - `disarm()` / `set_enabled(False)` -
        # and emergency stop still calls it. Nothing else may revoke press authority.
        # A run must not be reported as complete if cleanup threw.
        self.cleanup_ok = not report["errors"]
        self._record("cleanup", len(report["keys_released"]) + len(report["defensive_keys"]),
                     detail=f"errors={len(report['errors'])}")
        return report

    def restore_layout(self) -> dict[str, object]:
        """Restore the input language captured at ``begin_run``, if it moved."""
        result: dict[str, object] = {"restored": False, "reason": ""}
        if self.initial_layout is None:
            result["reason"] = "no baseline captured"
            return result
        current = read_layout()
        self.final_layout = current
        result["before"] = current.describe()
        result["baseline"] = self.initial_layout.describe()
        if current.hkl == self.initial_layout.hkl:
            result["reason"] = "layout unchanged"
            return result
        try:
            applied = set_layout(self.initial_layout.hkl)
            result["apply"] = applied
            result["after"] = applied.get("after")
            result["restored"] = bool(applied.get("applied"))
            result["reason"] = ("layout had moved; restored" if result["restored"]
                                else "restore attempted but layout did not settle")
        except Exception as exc:
            result["reason"] = f"restore failed: {exc}"
        self.final_layout = read_layout()
        return result

    # ---------------------------------------------------------------- verification

    def _make_record(self, *, outcome: str = "delivered", violation: str = "none",
                     foreground: dict | None = None, action_id: str = "") -> DispatchRecord:
        """Build a dispatch record directly.

        Exists so the audit-trail behaviour can be tested without a live desktop: whether
        an event reached the authorised target is a property of the record, and forcing a
        test to synthesise a whole input pipeline to check it would test nothing else.
        """
        self._seq += 1
        return DispatchRecord(
            mono_ms=self._now_ms(),
            event_id=self._seq,
            run_id=self.run_id,
            action_id=action_id or f"t{self._seq}",
            source="test",
            thread_id=0,
            pid=0,
            kind="synthetic",
            count=1,
            foreground=dict(foreground or {}),
            pressed_before=(),
            pressed_after=(),
            violation=str(violation),
            outcome=outcome,
        )

    def target_integrity(self) -> dict[str, object]:
        """Did every delivered event actually land on the bound target?

        This is the check that turns input verification from an *absence* claim ("nothing is
        left held") into a positive one ("every keystroke we sent went to the window we were
        authorised to drive").

        It matters because of what incidents 1 and 2 actually were: not "the mouse press was
        wrong" but "input reached somewhere it should never have reached". The evidence for
        that was being collected and never checked - every dispatch record carries the
        foreground window at the moment of the event, and nothing looked at it.
        """
        delivered = [r for r in self._records
                     if r.outcome == "delivered" and self.target_hwnd]
        if not self.target_hwnd:
            return {
                "checked": 0, "violations": 0, "clean": True,
                "note": "no target window was bound; the policy layer refuses all input in "
                        "that state, so nothing could be delivered off-target",
            }
        if not delivered:
            return {"checked": 0, "violations": 0, "clean": True,
                    "note": "no input was dispatched during this run"}

        violations: list[dict[str, object]] = []
        for record in delivered:
            fg_hwnd = record.foreground.get("hwnd")
            if fg_hwnd == self.target_hwnd:
                continue
            violations.append({
                "event_id": record.event_id,
                "action_id": record.action_id,
                "event": record.kind,
                "source": record.source,
                "expected_hwnd": self.target_hwnd,
                "actual_foreground_hwnd": fg_hwnd,
                "actual_foreground": (f"{record.foreground.get('process_name','')}"
                                      f"/{record.foreground.get('class_name','')}"),
            })
        return {
            "checked": len(delivered),
            "violations": len(violations),
            "clean": not violations,
            "target_hwnd": self.target_hwnd,
            "examples": violations[:5],
        }

    def post_run_check(self) -> dict[str, object]:
        """Non-invasive health check. Never types into a user document or dialog."""
        stuck = {k: v for k, v in stuck_modifiers().items() if v}
        layout = read_layout()
        integrity = self.target_integrity()
        check: dict[str, object] = {
            "target_integrity": integrity,
            "no_key_left_down": not self._held,
            "keys_still_down": sorted(self._held),
            "no_button_left_down": not self._held,
            "buttons_still_down": sorted(self._held),
            "cleanup_ok": self.cleanup_ok,
            "watchdog_releases": list(self.watchdog_releases),
            "closables_stopping": [n for n, _h in self._closables],
            "os_modifiers_down": stuck,
            "layout_matches_baseline": (
                self.initial_layout is not None and layout.hkl == self.initial_layout.hkl
            ),
            "layout_now": layout.describe(),
            "layout_baseline": (self.initial_layout.describe()
                                if self.initial_layout else None),
            "dispatch_violations": self.violations,
            "last_violation": self.last_violation_detail,
            "cleanups_run": self.cleanups,
        }
        # Healthy means all four of: nothing left held, no stuck OS modifiers, the input
        # language unchanged, cleanup succeeded - AND no input reached a window other than
        # the one we were authorised to drive.
        check["healthy"] = (
            not self._held
            and not stuck
            and check["layout_matches_baseline"]
            and self.cleanup_ok
            and bool(integrity.get("clean"))
        )
        return check

    def audit(self) -> dict[str, object]:
        """What this manager believes it did. Written into the run directory."""
        return {
            "dispatches": len(self._records),
            "violations": self.violations,
            #: A non-zero value here means presses were dropped by the arm gate. This is
            #: surfaced rather than buried because the first live run reported a plausible
            #: "7 pass / 2 fail" while silently discarding half its primitives.
            "blocked_presses": self.blocked_presses,
            "cleanups": self.cleanups,
            "initial_layout": (self.initial_layout.to_dict() if self.initial_layout else None),
            "final_layout": (self.final_layout.to_dict() if self.final_layout else None),
            "held_at_audit": sorted(self._held),
            "cleanup_ok": self.cleanup_ok,
            "watchdog_releases": list(self.watchdog_releases),
            "verbose_events": self.verbose_events,
            "allow_right_click": self.allow_right_click,
            "target_hwnd": self.target_hwnd,
            "require_target_foreground": self.require_target_foreground,
            "events": [
                {
                    "event_id": r.event_id, "mono_ms": r.mono_ms, "run_id": r.run_id,
                    "action_id": r.action_id, "source": r.source, "thread_id": r.thread_id,
                    "pid": r.pid, "event": r.kind, "count": r.count,
                    "target_window": r.target, "foreground_window": r.foreground,
                    "pressed_state_before": list(r.pressed_before),
                    "pressed_state_after": list(r.pressed_after),
                    "violation": r.violation, "result": r.outcome, "detail": r.detail,
                }
                for r in self._records[-200:]
            ],
            "recent": [
                {"kind": r.kind, "count": r.count, "violation": r.violation, "detail": r.detail}
                for r in self._records[-40:]
            ],
        }

    def write_audit(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"audit": self.audit(), "post_run_check": self.post_run_check()}
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path


def _vk_for(name: str) -> int | None:
    """Virtual-key code for a key name, via the input adapter's single source of truth."""
    from frameforge.adapters.input.sendinput import _VK

    try:
        from frameforge.ports.input import Key

        parsed = Key.parse(name)
    except (ValueError, AttributeError):
        return None
    return _VK.get(parsed)


def _callsite() -> str:
    """Identify the calling frame, so a bad event can be traced to a component."""
    import inspect

    for frame in inspect.stack()[2:6]:
        module = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if "safety.py" in module:
            continue
        return f"{module}:{frame.function}:{frame.lineno}"
    return "unknown"


def _keys_of(primitive) -> tuple[str, ...]:
    from frameforge.ports.input import PrimitiveType

    if primitive.kind is PrimitiveType.KEY and primitive.key is not None:
        return (str(primitive.key),)
    if primitive.kind is PrimitiveType.UNICODE:
        return ()
    return ()


# --------------------------------------------------------------- manual recovery


def recover_input_control(
    *,
    prefer_locale: int = 0x0409,
    prefer_layout: int = 0x0409,
    log_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Repair input state without restarting Windows.

    Runnable on a machine whose keyboard is compromised, which is the entire point: the
    previous defect required a restart, and this is the command that makes one
    unnecessary.

    Steps: report the current state, release every modifier and mouse button, re-activate
    the intended input layout, re-check, and write a log. It sends **no** keystrokes to
    any window, so it cannot type into a document or dismiss a dialog.
    """
    started = time.time()
    before_layout = read_layout()
    before_stuck = stuck_modifiers()
    available = list_layouts()

    result: dict[str, object] = {
        "started_at": started,
        "dry_run": dry_run,
        "before_layout": before_layout.describe(),
        "stuck_modifiers_before": {k: v for k, v in before_stuck.items() if v},
        "available_layouts": [f"0x{h:08X}" for h in available],
        "actions": [],
    }

    if dry_run:
        result["result"] = "dry run: nothing was changed"
        _finish(result, log_path)
        return result

    manager = InputSafetyManager()
    manager.initial_layout = before_layout
    # Refuse new input from the moment recovery starts.
    manager.enabled = False

    # 1. Release everything: tracked holds, then the full defensive sweep, including the
    #    context-menu key and F10, then every mouse button.
    try:
        release = manager.release_all()
        result["actions"].append({
            "release_all": {
                "keys_released": release.get("keys_released", []),
                "defensive_keys": release.get("defensive_keys", []),
                "buttons_released": release.get("buttons_released", []),
                "errors": release.get("errors", []),
            }
        })
        if release.get("errors"):
            result["errors_during_recovery"] = release["errors"]
    except Exception as exc:
        result["actions"].append({"release_all_error": str(exc)})
        result.setdefault("errors_during_recovery", []).append(str(exc))

    # 2. Re-activate the intended layout.
    target = None
    for h in available:
        if (h & 0xFFFF) == prefer_locale and ((h >> 16) & 0xFFFF) == prefer_layout:
            target = h
            break
    if target is None and available:
        target = available[0]
    if target is not None:
        try:
            result["actions"].append({"set_layout": set_layout(target)})
        except Exception as exc:
            result["actions"].append({"activate_error": str(exc)})

    after_layout = read_layout()
    # Filter before the truthiness test: stuck_modifiers() reports every modifier by name,
    # so an unfiltered dict is always truthy and would report "degraded" forever.
    after_stuck = {k: v for k, v in stuck_modifiers().items() if v}
    result["after_layout"] = after_layout.describe()
    result["intended_layout"] = f"0x{target:08X}" if target is not None else None
    result["stuck_modifiers_after"] = after_stuck
    result["layout_changed"] = after_layout.hkl != before_layout.hkl

    # "Clean" means: no modifier is logically down, and the active layout is the one we
    # intended - which is the intended locale if we switched, otherwise whatever was
    # already active. Comparing against the *pre-recovery* layout would be wrong: the
    # whole point of recovery is that the pre-recovery layout may be the broken one.
    layout_ok = (after_layout.hkl == target) if target is not None else True
    healthy = not after_stuck and layout_ok
    result["healthy"] = healthy
    result["result"] = (
        "input state is clean"
        if healthy
        else ("input state still degraded: " + "; ".join(filter(None, [
            f"stuck={list(result['stuck_modifiers_after'])}" if after_stuck else "",
            f"layout {after_layout.describe()} != intended {result['intended_layout']}"
            if not layout_ok else "",
        ])))
    )
    _finish(result, log_path)
    return result


def _finish(result: dict[str, object], log_path: Path | None) -> None:
    result["finished_at"] = time.time()
    if log_path is not None:
        p = Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        result["log_path"] = str(p)
        print(f"recovery log: {p}")


def apply_persistent_default_layout(input_tip: str = "0409:00000409") -> dict[str, object]:
    """Durably set the user's default input method. **Opt-in; never called by a run.**

    ``ActivateKeyboardLayout`` and ``WM_INPUTLANGCHANGEREQUEST`` are thread/window scoped,
    so neither survives process exit - measured, not assumed. The only durable change is
    the user's default input method, which is a profile-level setting.

    This is deliberately *not* part of a run's cleanup. Changing how someone's computer
    interprets their keystrokes is a user decision, not something an automation run should
    do behind their back. It is offered here, called only from
    ``frameforge recover-input --persist-default``, and the command says what it did.
    """
    import subprocess

    script = (
        f"$ErrorActionPreference='Stop';"
        f"$l = Get-WinUserLanguageList;"
        f"$l[0].InputMethodTips = @('{input_tip}');"
        f"Set-WinUserLanguageList $l -Force;"
        f"Write-Output 'applied'"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
            env=hardened_child_env(),
        )
    except Exception as exc:
        return {"applied": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "applied": proc.returncode == 0 and "applied" in (proc.stdout or ""),
        "input_tip": input_tip,
        "stdout": (proc.stdout or "").strip()[:200],
        "stderr": (proc.stderr or "").strip()[:400],
    }


#: Environment variables that let a third party inject code into a child process.
#:
#: Adopted from Codex's ``process-hardening`` crate, which strips these before ``main()``.
#: Frame Forge launches real executables - the target application and a PowerShell OCR host
#: - and previously handed them its environment verbatim. On a shared machine, anything
#: that can set an environment variable could subvert a spawned helper.
UNSAFE_ENV_VARS: tuple[str, ...] = (
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "NODE_OPTIONS", "RUST_LOG",
)


def hardened_child_env(base: dict[str, str] | None = None,
                       extra: dict[str, str] | None = None) -> dict[str, str]:
    """A child environment with injection-capable variables removed.

    Returns a copy; the current process environment is never mutated. Anything explicitly
    passed in ``extra`` is preserved, because a caller that deliberately sets one has made a
    decision we should not silently override.
    """
    env = dict(base if base is not None else os.environ)
    for name in UNSAFE_ENV_VARS:
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def audit_current_env() -> dict[str, object]:
    """Report which unsafe variables are currently set. Read-only."""
    present = [name for name in UNSAFE_ENV_VARS if os.environ.get(name)]
    return {
        "present": present,
        "clean": not present,
        "detail": (f"set in this environment: {present}. They are stripped from every "
                   f"child Frame Forge spawns."
                   if present else "no injection-capable variables are set"),
    }


__all__ = [
    "UNSAFE_ENV_VARS",
    "apply_persistent_default_layout",
    "audit_current_env",
    "hardened_child_env",
    "classify_target",
    "process_display_name",
    "process_image_path_for",
    "read_foreground_window",
    "DENIED_CLASSES",
    "DENIED_PROCESSES",
    "MENU_TRIGGER_KEYS",
    "DispatchRecord",
    "InputSafetyManager",
    "LayoutState",
    "MODIFIER_VKS",
    "Violation",
    "is_key_down",
    "list_layouts",
    "read_layout",
    "set_layout",
    "cursor_position",
    "virtual_size",
    "recover_input_control",
    "stuck_modifiers",
]
