"""Registered test target sessions, window-under-point verification, and the protected-
window registry.

This is the authorisation layer. Three things live here:

``TestTargetSession``
    A live input action is only permitted while a registered, non-expired session exists.
    The session binds a run to one verified window: hwnd, pid, executable image path,
    client and screen rectangles, monitor and DPI identity, the regions a scenario may
    touch, and a display-topology fingerprint.

``verify_point``
    ``WindowFromPoint`` before the cursor moves and again immediately before button-down.
    Foreground validation is *not* sufficient: it proves which window has focus, not which
    window is actually under the pixel being clicked. A notification, a context menu, an
    unrelated window moved by the user, or the agent's own UI will all pass a foreground
    check and still swallow the click.

``ProtectedRegistry``
    Windows the agent must never touch, tracked by **ownership** (pid/hwnd) rather than
    title or class text, so renaming or resizing does not defeat it.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum

from frameforge.actions.coordinates import ScreenPx, VirtualDesktop, assert_in_bounds
from frameforge.ports.geometry import Rect, Size

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GA_ROOT = 2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CMONITORS = 80
MDT_EFFECTIVE_DPI = 0

user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
# GetDpiForWindow lives in user32 (Win10 1607+). Probing shcore for it - a natural guess -
# raises AttributeError on this machine, because shcore only exports GetDpiForMonitor.
user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
user32.GetDpiForWindow.restype = wintypes.UINT

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def enable_per_monitor_dpi() -> bool:
    """Make this process per-monitor DPI aware.

    Without this, Windows virtualises coordinates for unaware processes, so a window rect
    reported to us and the pixels we capture can disagree by the scale factor - and a click
    computed from one lands in the other.
    """
    try:
        return bool(user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ))
    except Exception:
        return False


def virtual_desktop() -> VirtualDesktop:
    """Read the virtual screen rectangle. Origin may be negative."""
    return VirtualDesktop(
        x=int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        y=int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        width=max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))),
        height=max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))),
    )


def dpi_for_window(hwnd: int) -> int:
    """Per-window DPI, or 96 when unavailable.

    Never raises: a missing API must degrade to the default rather than fail a run. A wrong
    DPI is a rendering inaccuracy; an exception here is a dead run.
    """
    try:
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
        return dpi if dpi > 0 else 96
    except Exception:
        return 96


def window_identity(hwnd: int) -> dict[str, object]:
    """hwnd, pid, title, class. Never raises - a vanished window is normal on a live desktop."""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    title_buf = ctypes.create_unicode_buffer(512)
    class_buf = ctypes.create_unicode_buffer(256)
    try:
        user32.GetWindowTextW(wintypes.HWND(hwnd), title_buf, 512)
        user32.GetClassNameW(wintypes.HWND(hwnd), class_buf, 256)
    except Exception:
        pass
    return {
        "hwnd": int(hwnd),
        "pid": int(pid.value),
        "title": title_buf.value,
        "class_name": class_buf.value,
    }


def window_root(hwnd: int) -> int:
    """The top-level ancestor, so a click landing on a child control is still the target."""
    root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
    return int(root) if root else int(hwnd)


def window_at_point(point: ScreenPx) -> dict[str, object] | None:
    """Which window is physically under ``point``, in virtual-desktop pixels.

    The check that makes "did the click land where it was supposed to" answerable rather
    than assumed.
    """
    if not user32.WindowFromPoint(wintypes.POINT(int(point.x), int(point.y))):
        return None
    hwnd = int(user32.WindowFromPoint(wintypes.POINT(int(point.x), int(point.y))))
    info = window_identity(hwnd)
    root = window_root(hwnd)
    root_info = window_identity(root) if root != hwnd else info
    info["root_hwnd"] = root
    info["root_pid"] = root_info.get("pid")
    info["root_class"] = root_info.get("class_name")
    info["root_title"] = root_info.get("title")
    return info


class Verdict(StrEnum):
    ALLOW = "allow"
    NO_SESSION = "no_registered_target_session"
    EXPIRED = "target_session_expired"
    HWND_GONE = "target_window_missing"
    IDENTITY_CHANGED = "target_identity_changed"
    PROCESS_CHANGED = "target_pid_changed"
    IMAGE_CHANGED = "target_executable_changed"
    MINIMIZED = "target_minimized"
    MOVED = "target_moved_or_resized"
    MONITOR_CHANGED = "target_monitor_changed"
    DPI_CHANGED = "target_dpi_changed"
    TOPOLOGY_CHANGED = "display_topology_changed"
    OUT_OF_BOUNDS = "point_outside_virtual_desktop"
    NOT_IN_REGION = "point_not_in_target_region"
    PROTECTED = "protected_window_at_point"
    WRONG_WINDOW = "window_under_point_is_not_target"
    TARGET_GONE = "target_window_closed"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    verdict: Verdict
    detail: str = ""
    under_point: dict[str, object] | None = None
    foreground: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "detail": self.detail,
            "under_point": self.under_point,
            "foreground": self.foreground,
        }


@dataclass(slots=True)
class TargetSession:
    """A registered, verified window a run is permitted to drive.

    Every field is re-verified before each action. A window can move, resize, minimise,
    close, change monitor, change DPI, or be covered between planning and dispatch - and
    each of those invalidates the plan.
    """

    run_id: str
    hwnd: int
    pid: int
    #: Executable name as the OS reports it. Present because the policy layer validates
    #: identity against it; a session that could not say what program it authorises would
    #: leave policy and guard disagreeing about what a target is.
    process_name: str = ""
    image_path: str = ""
    title: str = ""
    class_name: str = ""
    client_origin: tuple[int, int] = (0, 0)
    client_size: Size = field(default_factory=lambda: Size(1280, 720))
    screen_rect: Rect | None = None
    monitor_index: int = 0
    monitor_device: str = ""
    dpi: int = 96
    #: Normalised regions a scenario may click, in client-relative fractions.
    approved_regions: tuple[tuple[float, float, float, float], ...] = ((0.0, 0.0, 1.0, 1.0),)
    topology_fingerprint: str = ""
    require_foreground: bool = True
    max_age_ms: float = 3_600_000.0
    created_mono_ms: float = 0.0
    #: If set, this pid's windows are always protected, even mid-run.
    protected_pids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not self.created_mono_ms:
            self.created_mono_ms = time.perf_counter() * 1000.0
        if not self.image_path:
            from frameforge.actions.safety import process_display_name

            self.image_path = process_display_name(self.pid)
        if not self.process_name and self.image_path:
            import os as _os

            self.process_name = _os.path.basename(self.image_path)

    @property
    def client_rect(self) -> Rect:
        return Rect(self.client_origin[0], self.client_origin[1],
                    self.client_size.width, self.client_size.height)

    @property
    def age_ms(self) -> float:
        return time.perf_counter() * 1000.0 - self.created_mono_ms

    def region_rects(self) -> tuple[Rect, ...]:
        w, h = self.client_size.width, self.client_size.height
        return tuple(Rect.from_norm(x, y, rw, rh, Size(w, h)) for x, y, rw, rh in self.approved_regions)

    def in_approved_region(self, client_x: int, client_y: int) -> bool:
        point = Point(client_x, client_y)
        return any(r.contains(point) for r in self.region_rects())

    def describe(self) -> str:
        return (f"session(run={self.run_id}, hwnd={self.hwnd}, pid={self.pid}, "
                f"class={self.class_name!r}, client={self.client_size.as_tuple()}, "
                f"origin={self.client_origin}, monitor={self.monitor_index}@{self.monitor_device}, "
                f"dpi={self.dpi}, age={self.age_ms:.0f}ms)")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id, "hwnd": self.hwnd, "pid": self.pid,
            "process_name": self.process_name,
            "image_path": self.image_path, "title": self.title, "class_name": self.class_name,
            "client_origin": list(self.client_origin),
            "client_size": self.client_size.as_tuple(),
            "screen_rect": self.screen_rect.as_tuple() if self.screen_rect else None,
            "monitor_index": self.monitor_index, "monitor_device": self.monitor_device,
            "dpi": self.dpi,
            "approved_regions": [list(r) for r in self.approved_regions],
            "topology_fingerprint": self.topology_fingerprint,
            "require_foreground": self.require_foreground,
            "max_age_ms": self.max_age_ms,
            "age_ms": round(self.age_ms, 1),
        }


class TargetGuard:
    """Validates an intended point against a session and the live desktop.

    Deliberately separate from :class:`InputPolicy`: the policy decides *permission*,
    this decides *whether the world still matches the plan*. Both must pass.
    """

    def __init__(self, session: TestTargetSession | None = None,
                 desktop: VirtualDesktop | None = None,
                 topology_fingerprint: str = "") -> None:
        self.session = session
        self.desktop = desktop or virtual_desktop()
        self.topology_fingerprint = topology_fingerprint

    def register(self, session: TestTargetSession) -> None:
        self.session = session

    def clear(self) -> None:
        self.session = None

    # ------------------------------------------------------------- session checks

    def validate_session(self) -> ValidationResult:
        s = self.session
        if s is None:
            return ValidationResult(Verdict.NO_SESSION,
                                   "no target session is registered; live input is refused")
        if s.age_ms > s.max_age_ms:
            return ValidationResult(Verdict.EXPIRED,
                                   f"target session is {s.age_ms:.0f}ms old "
                                   f"(max {s.max_age_ms:.0f}ms)")
        if not user32.IsWindow(wintypes.HWND(s.hwnd)):
            return ValidationResult(Verdict.HWND_GONE,
                                   f"target window {s.hwnd} no longer exists")
        if user32.IsIconic(wintypes.HWND(s.hwnd)):
            return ValidationResult(Verdict.MINIMIZED, "target window is minimised")

        identity = window_identity(s.hwnd)
        if identity["pid"] != s.pid:
            return ValidationResult(
                Verdict.PROCESS_CHANGED,
                f"window {s.hwnd} is now pid {identity['pid']}, expected {s.pid}",
                under_point=identity,
            )

        current = self._refresh_rect(s.hwnd)
        if current is not None and s.client_origin != current[0]:
            return ValidationResult(
                Verdict.MOVED,
                f"target client origin moved from {s.client_origin} to {current[0]} "
                "after the action was planned",
            )

        dpi = dpi_for_window(s.hwnd)
        if dpi != s.dpi:
            return ValidationResult(Verdict.DPI_CHANGED,
                                   f"target DPI changed from {s.dpi} to {dpi}")

        if self.topology_fingerprint:
            from frameforge.adapters.window.pywin32_window import monitor_topology_fingerprint

            now = monitor_topology_fingerprint()
            if now != self.topology_fingerprint:
                return ValidationResult(
                    Verdict.TOPOLOGY_CHANGED,
                    "display topology changed since the session was registered "
                    f"({self.topology_fingerprint} -> {now}); pending input must be "
                    "cancelled and the target revalidated",
                )
        return ValidationResult(Verdict.ALLOW)

    @staticmethod
    def _refresh_rect(hwnd: int) -> tuple[tuple[int, int], Size] | None:
        try:
            from frameforge.adapters.window.pywin32_window import client_origin_and_size

            return client_origin_and_size(hwnd)
        except Exception:
            return None

    # -------------------------------------------------------------- point checks

    def validate_point(self, screen_point: ScreenPx, *,
                        check_window_under_point: bool = True) -> ValidationResult:
        """Everything that must hold at ``screen_point`` immediately before dispatch."""
        session_check = self.validate_session()
        if not session_check.ok:
            return session_check
        s = self.session
        assert s is not None

        try:
            assert_in_bounds(screen_point, self.desktop, what="click point")
        except ValueError as exc:
            return ValidationResult(Verdict.OUT_OF_BOUNDS, str(exc))

        client_x = screen_point.x - s.client_origin[0]
        client_y = screen_point.y - s.client_origin[1]
        if not s.in_approved_region(client_x, client_y):
            return ValidationResult(
                Verdict.NOT_IN_REGION,
                f"point {client_x},{client_y} (client-relative) is outside every approved "
                f"region {[r.as_tuple() for r in s.region_rects()]}",
            )

        if not check_window_under_point:
            return ValidationResult(Verdict.ALLOW)

        under = window_at_point(screen_point)
        fg = window_identity(int(user32.GetForegroundWindow() or 0))
        if under is None:
            return ValidationResult(Verdict.WRONG_WINDOW,
                                   "no window is under the intended point",
                                   foreground=fg)

        under_pid = under.get("root_pid")
        if under_pid in s.protected_pids:
            return ValidationResult(
                Verdict.PROTECTED,
                f"the window under the point is pid {under_pid} "
                f"({under.get('root_title')!r}), which is protected",
                under_point=under, foreground=fg,
            )

        under_root = int(under.get("root_hwnd") or 0)
        allowed_owned = under_root in {s.hwnd} or under.get("pid") == s.pid
        if not allowed_owned:
            return ValidationResult(
                Verdict.WRONG_WINDOW,
                f"the window under the point is hwnd={under_root} pid={under_pid} "
                f"({under.get('root_title')!r}), not the registered target "
                f"hwnd={s.hwnd} pid={s.pid}. Something is covering it.",
                under_point=under, foreground=fg,
            )

        if s.require_foreground:
            fg_root = window_root(int(fg["hwnd"]))
            if fg_root not in {s.hwnd} and fg["pid"] != s.pid:
                return ValidationResult(
                    Verdict.WRONG_WINDOW,
                    f"foreground is hwnd={fg['hwnd']} pid={fg['pid']} "
                    f"({fg.get('title')!r}), not the registered target",
                    under_point=under, foreground=fg,
                )
        return ValidationResult(Verdict.ALLOW, under_point=under, foreground=fg)


class ProtectedRegistry:
    """Windows the agent must never inject into, tracked by ownership not title.

    The deny-list in ``safety`` is name-based and can drift with a title or class change.
    This is process- and hwnd-based, so the agent's own UI stays protected even when it is
    renamed, moved, resized, or showing a context menu.
    """

    def __init__(self) -> None:
        self._pids: set[int] = set()
        self._hwnds: set[int] = set()
        self._classes: set[str] = set()

    def protect_pid(self, pid: int) -> None:
        self._pids.add(int(pid))

    def protect_current_process(self) -> int:
        pid = os.getpid()
        self._pids.add(pid)
        return pid

    def protect_hwnd(self, hwnd: int) -> None:
        self._hwnds.add(int(hwnd))

    def protect_windows_of(self, pid: int) -> int:
        """Register every top-level window currently owned by ``pid``."""
        count = 0
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd, _lparam):
            identity = window_identity(int(hwnd))
            if identity["pid"] == int(pid):
                self._hwnds.add(int(hwnd))
                count += 1
            return True

        try:
            user32.EnumWindows(EnumWindowsProc(collect), 0)
        except Exception:
            pass
        return count

    def protect_class(self, class_name: str) -> None:
        self._classes.add(class_name.lower())

    def is_protected(self, hwnd: int | None, pid: int | None = None) -> tuple[bool, str]:
        if pid is not None and int(pid) in self._pids:
            return True, f"pid {pid} is registered as protected"
        if hwnd is not None and int(hwnd) in self._hwnds:
            return True, f"hwnd {hwnd} is registered as protected"
        if hwnd:
            identity = window_identity(int(hwnd))
            if identity.get("pid") in self._pids:
                return True, f"hwnd {hwnd} belongs to protected pid {identity['pid']}"
            cls = str(identity.get("class_name") or "").lower()
            if cls and cls in self._classes:
                return True, f"window class {cls!r} is registered as protected"
        return False, ""

    def report(self) -> dict[str, object]:
        return {
            "protected_pids": sorted(self._pids),
            "protected_hwnds": sorted(self._hwnds),
            "protected_classes": sorted(self._classes),
        }


from frameforge.ports.geometry import Point  # noqa: E402  (used in in_approved_region)

#: The specification calls this a ``TestTargetSession``. The class is named
#: ``TargetSession`` because pytest collects any identifier beginning with ``Test`` and would
#: otherwise try to instantiate it as a test class (it has required constructor arguments, so
#: collection errors). ``__test__ = False`` suppresses that for the alias as well, and the
#: spec's name stays available and greppable.
TargetSession.__test__ = False
TestTargetSession = TargetSession

__all__ = [
    "ProtectedRegistry",
    "TargetSession",
    "TestTargetSession",
    "TargetGuard",
    "TestTargetSession",
    "ValidationResult",
    "Verdict",
    "dpi_for_window",
    "enable_per_monitor_dpi",
    "virtual_desktop",
    "window_at_point",
    "window_identity",
    "window_root",
]
