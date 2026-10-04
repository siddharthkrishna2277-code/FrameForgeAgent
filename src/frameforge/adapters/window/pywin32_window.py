"""pywin32 window, monitor, session and human-input adapter.

This class carries most of the safety surface, so several methods are more defensive
than they first need to be:

* ``foreground()`` is polled before *every* input batch. It must never cache.
* ``find_window`` refuses an unidentified spec. Attaching to "whatever is focused" is the
  least safe behaviour available, so it is refused rather than warned about.
* ``set_foreground`` is best-effort and returns a bool. Windows denies foreground
  changes across some boundaries; callers must treat failure as normal, not exceptional.
* ``idle_ms`` is the human-input signal. It is sampled, not consumed - Frame Forge
  itself generates input that resets the counter, so the detector compares a baseline
  captured before each action rather than reading a raw "someone pressed something".
"""

from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes

from frameforge.actions.safety import (
    hardened_child_env,
    process_display_name,
    process_image_path_for,
)
from frameforge.ports.geometry import Point, Rect
from frameforge.ports.window import (
    DisplayTopology,
    MonitorInfo,
    SessionState,
    TargetSpec,
    WindowInfo,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CMONITORS = 80

MONITORINFOF_PRIMARY = 0x00000001

#: GetAncestor / GetWindow flags. Not provided by ctypes.wintypes, so defined here.
GA_ROOT = 2
GA_OWNER = 4


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


#: EnumDisplayMonitors passes FOUR callback args (HMONITOR, HDC, LPRECT, LPARAM).
MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
    ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
)

#: EnumWindows passes TWO (HWND, LPARAM). Using the monitor signature here silently
#: invokes the wrong prototype and yields an empty enumeration - which is exactly what
#: the hardware probe caught.
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def disambiguate(matches: list[WindowInfo], spec: TargetSpec) -> WindowInfo | None:
    """Reduce several matching windows to one, or refuse.

    Pure and module-level so the decision is testable without a live desktop: which
    window Frame Forge is about to type into must never be a coin flip.

    Escalation order is fixed and deliberate:

    1. exactly one match - use it;
    2. an exact class-name match, if that is unique;
    3. the foreground window, when the spec asks for it and it is unique;
    4. a *full* title-regex match, if unique (anchoring on the whole title is how a
       profile distinguishes two windows of the same class);
    5. otherwise raise, naming the candidates.
    """
    from frameforge.kernel.errors import AmbiguousTargetError

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    if spec.class_name:
        exact = [m for m in matches if m.class_name == spec.class_name]
        if len(exact) == 1:
            return exact[0]

    if spec.require_foreground:
        fg = [m for m in matches if m.is_foreground]
        if len(fg) == 1:
            return fg[0]

    if spec.title_regex:
        import re as _re

        pattern = _re.compile(spec.title_regex, _re.IGNORECASE)
        exact = [m for m in matches if pattern.fullmatch(m.title)]
        if len(exact) == 1:
            return exact[0]

    # Genuinely ambiguous. Refuse, and say which windows - a bare "not found" sends the
    # operator hunting for a missing window that is in fact present and perfectly visible.
    raise AmbiguousTargetError(
        [f"{m.title!r} (pid={m.pid}, class={m.class_name})" for m in matches]
    )


#: Set once, at import, before any window or capture work happens.
#:
#: DPI awareness must be established *before* the first window or capture call, or Windows
#: virtualises coordinates for an unaware process: a rect reported to us and the pixels we
#: capture can disagree by the scale factor, and a click computed from one lands in the
#: other. On mixed-scaling hardware that is a systematically wrong click, not a rounding
#: error.
_DPI_AWARENESS: dict[str, object] = {"attempted": False, "active": False, "mode": None}


def enable_dpi_awareness() -> dict[str, object]:
    """Make this process per-monitor DPI aware. Idempotent; safe to call repeatedly.

    Tries the most capable context first and falls back, because availability varies by
    Windows build. Returns what actually happened, so the caller can report it rather than
    assume it.
    """
    if _DPI_AWARENESS["attempted"]:
        return dict(_DPI_AWARENESS)
    _DPI_AWARENESS["attempted"] = True

    # Windows 10 1703+: per-monitor v2 - the only mode that gives correct non-client
    # rects for a window on a differently-scaled display.
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARENESS.update(active=True, mode="per_monitor_v2")
            return dict(_DPI_AWARENESS)
    except Exception:
        pass

    # Windows 8.1+: per-monitor v1.
    try:
        if user32.SetProcessDpiAwareness(2) == 0:      # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE
            _DPI_AWARENESS.update(active=True, mode="per_monitor_v1")
            return dict(_DPI_AWARENESS)
    except Exception:
        pass

    # Vista+: system aware. Better than nothing, but Windows still scales for us.
    try:
        if user32.SetProcessDPIAware() is not None:
            _DPI_AWARENESS.update(active=True, mode="system_aware")
            return dict(_DPI_AWARENESS)
    except Exception:
        pass

    _DPI_AWARENESS.update(active=False, mode="unaware")
    return dict(_DPI_AWARENESS)


def dpi_awareness() -> dict[str, object]:
    """Current DPI awareness state, enabling it first if nobody has."""
    if not _DPI_AWARENESS["attempted"]:
        return enable_dpi_awareness()
    return dict(_DPI_AWARENESS)


def monitor_topology_fingerprint() -> str:
    """A stable string identifying the current display arrangement.

    Compared before every dispatch: if the arrangement changed since the target session was
    registered, every cached rectangle, surface offset and normalised mapping is suspect and
    the action is rejected rather than computed from stale geometry.
    """
    from frameforge.actions.target import virtual_desktop

    desktop = virtual_desktop()
    parts = [f"vd={desktop.x},{desktop.y},{desktop.width},{desktop.height}"]
    try:
        adapter = PyWin32WindowAdapter()
        for m in adapter.monitors_detailed():
            # DPI is part of the identity: a scaling change alters the mapping between
            # client pixels and physical pixels, so every cached rectangle is suspect.
            parts.append(
                f"{m.index}:{m.device_name}:{m.rect.as_tuple()}:{m.dpi}:{m.scale_percent}"
                f":{int(m.is_primary)}"
            )
    except Exception:
        pass
    return "|".join(parts)


class PyWin32WindowAdapter:
    """Window identity, focus, monitors, session state, and input idleness."""

    name = "pywin32"

    def __init__(self) -> None:
        import win32api  # noqa: F401
        import win32con
        import win32gui
        import win32process

        self._gui = win32gui
        self._con = win32con
        self._process = win32process
        self._proc_name_cache: dict[int, str] = {}
        self._own_hwnds: set[int] = set()
        self._console_hwnd: int = 0
        self._own_cache_mono: float | None = None

        user32.GetLastInputInfo.argtypes = (ctypes.POINTER(_LASTINPUTINFO),)
        user32.GetLastInputInfo.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = (EnumWindowsProc, wintypes.LPARAM)
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = (
            wintypes.HDC, ctypes.POINTER(wintypes.RECT), MonitorEnumProc, wintypes.LPARAM
        )
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        # Foreground acquisition.
        user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        )
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        # NOTE: keybd_event is deliberately NOT declared or used anywhere. It was the
        # cause of a system-wide input-language corruption (see docs/AI_GUARDRAILS.md
        # G-ABS-10). Setting foreground uses SetWindowPos, which needs no keystroke.
        user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetWindow.restype = wintypes.HWND
        user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        self._idle_tick_ms = 1000
        # GetTickCount lives in kernel32, not user32 - and GetTickCount64 is preferable
        # because it does not wrap after ~49.7 days, which a machine left running would.
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetCurrentThreadId.argtypes = ()
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._kernel32.GetTickCount64.argtypes = ()
        self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong

    # ------------------------------------------------------------------ enumeration

    def enumerate_windows(self, visible_only: bool = False) -> list[WindowInfo]:
        """Enumerate top-level windows.

        The accumulator is captured by the callback closure rather than smuggled through
        ``lparam``: ctypes cannot build a pointer to a Python list, and passing a real
        pointer would require unmanaged memory whose lifetime we'd then have to manage.
        The declared prototype matters though - ``EnumWindows`` passes two callback
        arguments while ``EnumDisplayMonitors`` passes four, and using the wrong signature
        silently produces an empty enumeration.
        """
        out: list[WindowInfo] = []

        def collect(hwnd: int, _lparam: int) -> bool:
            info = self._describe(int(hwnd))
            if info is not None:
                out.append(info)
            return True

        user32.EnumWindows(EnumWindowsProc(collect), 0)
        if visible_only:
            return [w for w in out if w.is_visible]
        return out

    def _describe(self, hwnd: int) -> WindowInfo | None:
        try:
            title = self._gui.GetWindowText(hwnd)
            class_name = self._gui.GetClassName(hwnd)
            if not title and not class_name:
                return None
            _tid, pid = self._process.GetWindowThreadProcessId(hwnd)
            visible = bool(self._gui.IsWindowVisible(hwnd))
            minimized = bool(self._gui.IsIconic(hwnd))
            foreground_hwnd = self._gui.GetForegroundWindow()
            client = self._client_rect(hwnd)
            win_rect = self._window_rect(hwnd)
            origin = self._client_origin(hwnd)
            return WindowInfo(
                hwnd=int(hwnd),
                title=title,
                class_name=class_name,
                pid=int(pid),
                process_name=self._process_name(int(pid)),
                is_visible=visible,
                is_minimized=minimized,
                is_foreground=int(hwnd) == int(foreground_hwnd),
                client_rect=client,
                window_rect=win_rect,
                monitor_index=self._monitor_of(origin.x, origin.y),
                client_origin_screen=origin,
            )
        except Exception:
            # A window can vanish between enumeration and description. That is normal on
            # a live desktop and must not abort the sweep.
            return None

    def _client_rect(self, hwnd: int) -> Rect | None:
        """Client-area size in pixels.

        Capture and input both work in client-area space, so this is the surface that
        profile regions are expressed against. ``win32api.GetClientRect`` does not exist
        (it is not part of that module), so the Win32 call is made directly - it is
        declared with an explicit prototype so the 16-bit ``BOOL`` return does not truncate.
        """
        rc = wintypes.RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
            return None
        return Rect(0, 0, int(rc.right - rc.left), int(rc.bottom - rc.top))

    def _window_rect(self, hwnd: int) -> Rect | None:
        rc = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc)):
            return None
        return Rect(int(rc.left), int(rc.top), int(rc.right - rc.left), int(rc.bottom - rc.top))

    def _client_origin(self, hwnd: int) -> Point:
        """Top-left of the *client* area in screen coordinates.

        This is the origin that makes a surface-local click land where the image said it
        would. It matters here specifically: this machine has a second monitor at x=1920,
        so a window there needs offset (1920, 0) or every "correct" click lands on the
        primary display instead.
        """
        pt = wintypes.POINT(0, 0)
        if user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return Point(int(pt.x), int(pt.y))
        r = self._window_rect(hwnd)
        return Point(r.x, r.y) if r else Point(0, 0)

    def _process_name(self, pid: int) -> str:
        """Executable name for a pid, or "" if it cannot be read.

        Uses ``QueryFullProcessImageNameW`` via ctypes rather than win32process helpers:
        it is the documented Win32 entry point, it works under UAC without elevation for
        same-integrity processes, and it avoids a module-attribute trap (``OpenProcess``
        lives in ``win32api``, not ``win32process``).

        A failure returns "" rather than raising. A window whose process name is
        unreadable must still be enumerable - refusing to describe it would hide a target
        that Frame Forge could otherwise operate.
        """
        if pid in self._proc_name_cache:
            return self._proc_name_cache[pid]
        name = ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = self._kernel32
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        )
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    name = buf.value.split("\\")[-1]
            finally:
                kernel32.CloseHandle(handle)
        if not name:
            # Last resort: tasklist is slow but always present. Only reached when the
            # direct API is denied, which should be rare.
            name = self._process_name_tasklist(pid)
        self._proc_name_cache[pid] = name
        return name

    def _process_name_tasklist(self, pid: int) -> str:
        import re
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10, text=True,
                # Last-resort process-name lookup; hard like every other spawn.
                env=hardened_child_env(),
            ).stdout
            match = re.search(r'"([^"]+)"', out or "")
            return match.group(1) if match else ""
        except Exception:
            return ""

    # ----------------------------------------------------------------- resolution

    def find_window(self, spec: TargetSpec) -> WindowInfo | None:
        """Resolve ``spec`` to a single window.

        Refuses an unidentified spec outright (guardrail G-AUTH-04 spirit: fail closed on
        ambiguity). Requires *uniqueness*: two matching windows is an error condition,
        because picking the first would be a coin flip about what we are about to click.
        """
        if not spec.identifying:
            msg = "TargetSpec must identify its target (title/class/process/pid/exe)"
            raise ValueError(msg)

        pattern = re.compile(spec.title_regex, re.IGNORECASE) if spec.title_regex else None
        matches: list[WindowInfo] = []
        for info in self.enumerate_windows():
            if spec.require_visible and not info.is_visible:
                continue
            if spec.class_name and info.class_name != spec.class_name:
                continue
            if spec.process_name and info.process_name.lower() != spec.process_name.lower():
                continue
            if spec.exe_names and info.process_name.lower() not in {
                e.lower() for e in spec.exe_names
            }:
                continue
            if spec.pid and info.pid != spec.pid:
                continue
            if pattern and not pattern.search(info.title):
                continue
            matches.append(info)

        if not matches:
            return None
        return disambiguate(matches, spec)
        return matches[0]

    def window_info(self, hwnd: int) -> WindowInfo | None:
        return self._describe(hwnd)

    def foreground(self) -> WindowInfo | None:
        hwnd = self._gui.GetForegroundWindow()
        return self._describe(hwnd) if hwnd else None

    def set_foreground(self, hwnd: int, *, retries: int = 4) -> bool:
        """Bring a window to the front. Returns whether it took.

        Windows refuses ``SetForegroundWindow`` from a process that does not own the
        current foreground thread, and it refuses silently. A plain
        ``BringWindowToTop`` then leaves the window *behind* a composited foreground app -
        observed with the Frame Forge desktop window covering Notepad completely, while
        Frame Forge reported the target acquired.

        So this escalates through the techniques Windows actually honours, cheapest and
        least intrusive first:

        1. nothing to do if it is already foreground;
        2. restore if minimised;
        3. SetWindowPos to HWND_TOP and then immediately back off HWND_NOTOPMOST - the
           reliable way to raise a window above a DWM-composited one *without leaving it
           permanently on top*;
        4. AttachThreadInput plus SetForegroundWindow, for the cross-thread case;
        5. verify, and retry;
        **No keybd_event ALT tap. Ever.** An earlier version escalated to the classic
        ALT-tap trick to acquire foreground permission. It caused a system-wide input
        corruption: on a machine with both ``en-US`` and ``en-IN`` installed, the ALT tap
        activated the IME and switched the active input language to ``en-IN``, which
        persisted after every process exited. The user then saw ``N`` produce an
        alternate-script character, with Backspace and Delete dead, recoverable only by
        restarting Windows.

        SetWindowPos + AttachThreadInput is sufficient on its own - verified by raising a
        window out from under a DWM-composited foreground application, occlusion 100% ->
        0%. There is no reason to synthesise a keystroke to gain foreground, and a
        keystroke is the one thing in this codebase that can change machine-wide state the
        operator cannot see. See docs/AI_GUARDRAILS.md G-ABS-10.
        """
        if not hwnd:
            return False
        try:
            h = wintypes.HWND(hwnd)
            for attempt in range(max(1, retries)):
                if self._gui.GetForegroundWindow() == hwnd:
                    return True

                # Restore a minimised window first; SetWindowPos on a minimised window is a
                # no-op for z-order purposes.
                if self._gui.IsIconic(hwnd):
                    self._gui.ShowWindow(hwnd, self._con.SW_RESTORE)

                SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
                HWND_TOP, HWND_NOTOPMOST = 0, -2
                user32.SetWindowPos(h, wintypes.HWND(HWND_TOP), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE)
                user32.SetWindowPos(h, wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE)

                # Cross-thread fallback.
                current = self._gui.GetForegroundWindow()
                fg_thread = self._process.GetWindowThreadProcessId(current)[0] if current else 0
                my_thread = self._kernel32.GetCurrentThreadId()
                attached = bool(fg_thread and fg_thread != my_thread)
                if attached:
                    user32.AttachThreadInput(my_thread, fg_thread, True)
                try:
                    self._gui.BringWindowToTop(hwnd)
                    self._gui.SetForegroundWindow(hwnd)
                    self._gui.SetActiveWindow(hwnd)
                finally:
                    if attached:
                        user32.AttachThreadInput(my_thread, fg_thread, False)

                if self._gui.GetForegroundWindow() == hwnd:
                    return True
                time.sleep(0.15)
            return self._gui.GetForegroundWindow() == hwnd
        except Exception:
            return False

    # -------------------------------------------------------------------- monitors

    def client_origin_and_size(self, hwnd: int) -> tuple[tuple[int, int], Size]:
        """Client origin (screen px) and client size, in one call.

        Both are needed to convert between surface-local and virtual-desktop coordinates,
        and reading them separately risks observing a window mid-move.
        """
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
        rc = wintypes.RECT()
        user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rc))
        return (int(pt.x), int(pt.y)), Size(int(rc.right - rc.left), int(rc.bottom - rc.top))

    def monitors_detailed(self):
        """Monitors with work area, DPI, orientation and primary flag."""
        from frameforge.actions.coordinates import MonitorInfo as _MI

        infos: list[_MI] = []
        shcore = None
        try:
            # shcore exports GetDpiForMonitor (per-monitor). If it is unavailable we fall
            # back to the window's DPI, which is still per-monitor on a V2-aware process.
            shcore = ctypes.WinDLL("shcore")
            shcore.GetDpiForMonitor.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                ctypes.POINTER(wintypes.UINT),
                                                ctypes.POINTER(wintypes.UINT))
            shcore.GetDpiForMonitor.restype = ctypes.c_long
        except Exception:
            shcore = None

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        def callback(hmon, _hdc, _rect, _lparam):
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r, wk = mi.rcMonitor, mi.rcWork
                dpi = 96
                if shcore is not None:
                    x = wintypes.UINT(); y = wintypes.UINT()
                    try:
                        if shcore.GetDpiForMonitor(hmon, 0,
                                                   ctypes.byref(x), ctypes.byref(y)) == 0:
                            dpi = int(x.value) or 96
                    except Exception:
                        dpi = 96
                width = int(r.right - r.left)
                infos.append(_MI(
                    index=len(infos),
                    device_name=f"\\\\.\\DISPLAY{len(infos) + 1}",
                    rect=Rect(int(r.left), int(r.top), width, int(r.bottom - r.top)),
                    work_area=Rect(int(wk.left), int(wk.top),
                                   int(wk.right - wk.left), int(wk.bottom - wk.top)),
                    dpi=dpi,
                    orientation="landscape" if width >= int(r.bottom - r.top) else "portrait",
                    is_primary=bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                    scale_percent=round(dpi / 96.0 * 100),
                ))
            return True

        MonitorEnumProc2 = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.EnumDisplayMonitors(None, None, MonitorEnumProc2(callback), 0)
        return infos

    def process_image_path(self, pid: int) -> str:
        """Full executable path for a pid, or "".

        This is the *identity* Codex's execpolicy pins with ``host_executable(paths=[...])``.
        A process name is not an identity: any process can name itself ``notepad.exe``.
        A resolved image path is checkable - it exists on disk, and its basename must agree
        with the reported name. Returns the *path*, not the basename: the two were
        accidentally conflated, and identity verification correctly refused the result
        because ``Notepad.exe`` is not a path that exists anywhere.
        """
        return process_image_path_for(pid)

    def _own_window_ids(self) -> set[int]:
        """Top-level windows belonging to this process.

        Cached, because it is only needed to answer "is the thing covering my target
        actually me?" - and an agent must never blame itself for the occlusion.
        """
        if self._own_cache_mono is not None and (time.monotonic() - self._own_cache_mono) < 2.0:
            return self._own_hwnds
        import os

        me = os.getpid()
        self._own_hwnds = {
            w.hwnd for w in self.enumerate_windows() if w.pid == me
        }
        try:
            # GetConsoleWindow lives in kernel32, and a GUI-subsystem process has no console
            # at all - so this is probed rather than assumed.
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetConsoleWindow.restype = wintypes.HWND
            console = k32.GetConsoleWindow()
            self._console_hwnd = int(console) if console else 0
        except Exception:
            self._console_hwnd = 0
        self._own_cache_mono = time.monotonic()
        return self._own_hwnds

    def occlusion(self, hwnd: int, client_origin: Point, size: Size) -> OcclusionInfo:
        """Hit-test a grid over the client area to find what is actually on top.

        Uses ``WindowFromPoint`` (not ``ChildWindowFromPoint``): for occlusion we care
        about which *top-level* window owns the pixel, and a control belonging to the target
        is not an occluder. A grid of 5x5 is enough to distinguish "mostly visible" from
        "mostly covered" without making this expensive enough to matter in the loop.
        """
        from frameforge.ports.window import OcclusionInfo

        self._own_window_ids()
        if not hwnd or size.width <= 0 or size.height <= 0:
            return OcclusionInfo(occluded=False)

        cols, rows = 5, 5
        hit = 0
        total = 0
        titles: list[str] = []
        classes: list[str] = []
        for r in range(rows):
            for c in range(cols):
                # Sample inside each cell rather than at its edge, so a window that covers
                # only half a cell is still detected.
                fx = (c + 0.5) / cols
                fy = (r + 0.5) / rows
                x = client_origin.x + int(fx * size.width)
                y = client_origin.y + int(fy * size.height)
                top = user32.WindowFromPoint(wintypes.POINT(x, y))
                total += 1
                if not top:
                    continue
                if int(top) == int(hwnd):
                    continue
                # Walk up to the owning top-level window; a child control of the target
                # (or of a popup it owns) is not an occluder.
                root = user32.GetAncestor(wintypes.HWND(top), GA_ROOT)
                root_id = int(root or top)
                if root_id == int(hwnd):
                    continue
                # A window owned by the target (its own dialog) is not an occluder either.
                owner = user32.GetWindow(wintypes.HWND(root_id), GA_OWNER)
                if owner and int(owner) == int(hwnd):
                    continue
                # Our own console is not an occluder. Frame Forge launched from a console
                # will otherwise report *itself* as covering the target, and refuse to
                # perceive anything - self-inflicted occlusion that looks exactly like an
                # external problem. Observed against a live Notepad run: every step returned
                # UNKNOWN because the console sat on top of the window being tested.
                if root_id in self._own_hwnds or root_id == self._console_hwnd:
                    continue
                hit += 1
                title = self._gui.GetWindowText(int(root_id))
                klass = self._gui.GetClassName(int(root_id))
                if title and title not in titles:
                    titles.append(title)
                if klass and klass not in classes:
                    classes.append(klass)

        fraction = hit / total if total else 0.0
        return OcclusionInfo(
            occluded=fraction > 0.0,
            occluded_fraction=round(fraction, 3),
            covering_titles=tuple(titles[:4]),
            covering_classes=tuple(classes[:4]),
        )

    def monitors(self) -> DisplayTopology:
        infos: list[MonitorInfo] = []

        def callback(hmon, _hdc, _rect, _lparam) -> bool:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcMonitor
                infos.append(
                    MonitorInfo(
                        index=len(infos),
                        device_name=f"\\\\.\\DISPLAY{len(infos) + 1}",
                        rect=Rect(int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)),
                        is_primary=bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                    )
                )
            return True

        try:
            user32.EnumDisplayMonitors(None, None, MonitorEnumProc(callback), 0)
        except Exception:
            pass

        if not infos:
            # Fallback: assume a single 1920x1080 primary. Better a slightly wrong
            # topology than a crash, and doctor() reports the assumption.
            infos = [MonitorInfo(0, "\\\\.\\DISPLAY1", Rect(0, 0, 1920, 1080), True)]

        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        primary = next((m.index for m in infos if m.is_primary), 0)
        return DisplayTopology(
            monitors=tuple(infos),
            virtual_rect=Rect(int(vx), int(vy), int(vw), int(vh)),
            primary_index=primary,
        )

    def _monitor_of(self, x: int, y: int) -> int:
        top = self.monitors()
        for m in top.monitors:
            if m.rect.contains(Point(x, y)):
                return m.index
        return top.primary_index

    # ---------------------------------------------------------------- input idle

    def last_input_info(self) -> int:
        """Ticks since the last input of any kind, system-wide."""
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        # dwTime is a 32-bit tick count; mask it to the low 32 bits before comparing
        # against the 64-bit tick count, or the subtraction goes negative on a long uptime.
        now = self._kernel32.GetTickCount64()
        return int((now & 0xFFFFFFFF) - info.dwTime) & 0xFFFFFFFF

    def idle_ms(self) -> int:
        return self.last_input_info()

    def cursor_position(self) -> Point:
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return Point(int(pt.x), int(pt.y))
        return Point(0, 0)

    # -------------------------------------------------------------------- session

    def console_session_id(self) -> int:
        try:
            return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
        except Exception:
            return 1

    def session_state(self, confirmations: int = 2, gap_ms: int = 60) -> SessionState:
        """Best-effort interactivity check.

        ``GetForegroundWindow() == 0`` used to be read as "locked" immediately. It is not:
        the foreground window is null *transiently* whenever windows are being activated,
        and a newly launched application does exactly that. That made the session guard
        block a legitimate run - observed against a real Notepad launch, where the first
        keystroke was refused because the app had not finished appearing.

        So a null foreground must now *persist* across consecutive samples before it is
        believed. Refusing input on a genuine lock is still the correct direction; refusing
        it on a flicker is not.
        """
        try:
            sid = self.console_session_id()
            if sid == 0xFFFFFFFF:
                return SessionState.DISCONNECTED
        except Exception:
            pass

        null_streak = 0
        for _ in range(max(1, confirmations)):
            try:
                fg = user32.GetForegroundWindow()
            except Exception:
                return SessionState.UNKNOWN
            if fg == 0:
                null_streak += 1
                if null_streak < max(1, confirmations):
                    time.sleep(gap_ms / 1000.0)
                    continue
                return SessionState.LOCKED
            return SessionState.ACTIVE
        return SessionState.UNKNOWN

    def sleep_ms(self, ms: float) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)


__all__ = ["PyWin32WindowAdapter"]
