"""Window, focus, session and human-input port.

This port carries the entire safety surface for "is it still safe to act":

* ``find_window``  - target identity resolution (never cached blindly; a changed HWND
  for the same PID is a *new identity* and is treated as target loss).
* ``foreground``   - read every time before input.
* ``set_foreground`` - used only by an explicit re-acquire policy, never to steal focus
  back from the user without consent.
* ``last_input_info`` - human-input detection via ``GetLastInputInfo``.
* ``session_state`` - a disconnected or locked session must never receive synthetic
  input, because there is nobody there to stop it.

All of it is implemented by one pywin32 adapter plus a ``FakeWindowAdapter`` for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from frameforge.ports.geometry import Point, Rect


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """Identity and geometry of a top-level window."""

    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str
    is_visible: bool = True
    is_minimized: bool = False
    is_foreground: bool = False
    client_rect: Rect | None = None
    window_rect: Rect | None = None
    monitor_index: int = 0
    client_origin_screen: Point = Point(0, 0)

    def identity(self) -> tuple[int, str, int]:
        """The triple that defines "is this still the same window".

        Deliberately includes the HWND. Re-acquiring the same title in a new window is
        a *different* target and must be re-verified, not silently adopted.
        """
        return (self.pid, self.class_name, self.hwnd)

    def describe(self) -> str:
        return f"{self.process_name}[{self.pid}] {self.class_name!r} {self.title!r} hwnd={self.hwnd}"


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """How to find the target window. Match rules are OR'd within a field, AND'd across.

    Requiring at least one identifying field is a policy check, not a convenience: a
    spec that matches every window on the desktop would attach to whatever happened to
    be focused, which is the least safe possible behaviour.
    """

    title_regex: str | None = None
    class_name: str | None = None
    process_name: str | None = None
    pid: int | None = None
    exe_names: tuple[str, ...] = ()
    require_visible: bool = True
    require_foreground: bool = False

    @property
    def identifying(self) -> bool:
        return any(
            v not in (None, "", (), 0)
            for v in (
                self.title_regex,
                self.class_name,
                self.process_name,
                self.pid,
                self.exe_names,
            )
        )

    def describe(self) -> str:
        bits = []
        if self.title_regex:
            bits.append(f"title~{self.title_regex!r}")
        if self.class_name:
            bits.append(f"class={self.class_name!r}")
        if self.process_name:
            bits.append(f"proc={self.process_name!r}")
        if self.exe_names:
            bits.append(f"exe={list(self.exe_names)}")
        if self.pid:
            bits.append(f"pid={self.pid}")
        return ", ".join(bits) or "<unidentified>"


class SessionState(StrEnum):
    """Windows session interactivity."""

    ACTIVE = "active"
    LOCKED = "locked"
    DISCONNECTED = "disconnected"
    REMOTE = "remote"
    UNKNOWN = "unknown"

    @property
    def interactive(self) -> bool:
        """Only an interactive session may receive synthetic input (G-SES-01)."""
        return self in (SessionState.ACTIVE, SessionState.REMOTE)


@dataclass(frozen=True, slots=True)
class OcclusionInfo:
    """Whether the target window is covered by something else.

    Capturing a window's client rect returns whatever is *on screen* in that rect, not
    what the window would render. A notification, a tooltip, an overlapping window or a
    context menu therefore silently corrupts every frame - and because the pixels change,
    change-detection reports activity and every postcondition becomes unreliable.

    Discovered by capturing a real Notepad window that was partly behind a Control Panel
    window: OCR confidently read "Control Panel" from what should have been Notepad.
    """

    occluded: bool
    #: Fraction of sampled probe points that resolve to a different top-level window.
    occluded_fraction: float = 0.0
    #: Titles of the windows doing the occluding, for the health log.
    covering_titles: tuple[str, ...] = ()
    covering_classes: tuple[str, ...] = ()
    #: A popup belonging to the target itself was on screen (menu, tooltip, flyout). It is
    #: not a foreign occluder, but it *is* covering the pixels, so the run must say so
    #: rather than silently reading the popup.
    target_popup: bool = False


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """A display in the virtual desktop."""

    index: int
    device_name: str
    rect: Rect
    is_primary: bool
    dpi_scale: float = 1.0

    @property
    def width(self) -> int:
        return self.rect.width

    @property
    def height(self) -> int:
        return self.rect.height


@dataclass(frozen=True, slots=True)
class DisplayTopology:
    """The set of monitors, virtual-desktop bounds, and primary index.

    ``signature`` is what the health monitor compares between epochs: any change in
    monitors, resolution, or primary index yields a different signature and forces
    re-acquisition, because a cached ROI or cursor mapping would otherwise be wrong.
    """

    monitors: tuple[MonitorInfo, ...]
    virtual_rect: Rect
    primary_index: int = 0

    @property
    def signature(self) -> str:
        parts = [f"{m.index}:{m.device_name}:{m.rect.as_tuple()}:{'P' if m.is_primary else '-'}" for m in self.monitors]
        parts.append(f"virtual={self.virtual_rect.as_tuple()}")
        return "|".join(parts)

    def monitor(self, index: int) -> MonitorInfo | None:
        return next((m for m in self.monitors if m.index == index), None)

    @property
    def total_pixels(self) -> int:
        return sum(m.width * m.height for m in self.monitors)


@runtime_checkable
class WindowPort(Protocol):
    """Target identity, foreground tracking, session state, human input."""

    name: str

    def enumerate_windows(self) -> list[WindowInfo]:
        ...

    def find_window(self, spec: TargetSpec) -> WindowInfo | None:
        """Resolve a spec to exactly one window, or ``None``."""
        ...

    def window_info(self, hwnd: int) -> WindowInfo | None:
        ...

    def foreground(self) -> WindowInfo | None:
        ...

    def set_foreground(self, hwnd: int) -> bool:
        """Best-effort focus request. Returns success; failure is not fatal."""
        ...

    def monitors(self) -> DisplayTopology:
        ...

    def last_input_info(self) -> int:
        """Milliseconds since the last input of *any* kind (human or synthetic).

        Combined with ``GetLastInputInfo``'s tick count semantics, a jump in this value
        while the agent believes it is the only actor is the human-input signal.
        """
        ...

    def idle_ms(self) -> int:
        """Milliseconds of system-wide input idleness."""
        ...

    def session_state(self) -> SessionState:
        ...

    def console_session_id(self) -> int:
        ...


__all__ = [
    "DisplayTopology",
    "MonitorInfo",
    "SessionState",
    "TargetSpec",
    "WindowInfo",
    "WindowPort",
]
