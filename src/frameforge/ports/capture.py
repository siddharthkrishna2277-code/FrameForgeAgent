"""Capture port.

A capture source yields timestamped frames for a *target surface*: a monitor, a window,
or a rectangular region. It performs no interpretation whatsoever - no OCR, no matching,
no classification. Perception is a separate concern that happens to consume frames.

Two implementations ship:

* ``adapters/capture/mss_capture.py`` - default. BitBlt based, ~1 MB dependency, works
  on every configuration including RDP and some VM display drivers.
* ``adapters/capture/dxcam_capture.py`` - optional. Desktop Duplication, hardware path,
  lower latency, sensitive to driver quirks.

Because both sit behind this port, promoting DXGI is a configuration flag rather than a
redesign, and P0-P3 are never blocked on a DXGI problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np

from frameforge.ports.geometry import Rect, Size


class SurfaceKind(StrEnum):
    """What a frame represents. Decides how a frame is located on the real desktop."""

    MONITOR = "monitor"
    WINDOW = "window"
    REGION = "region"
    DESKTOP = "desktop"


@dataclass(frozen=True, slots=True)
class Surface:
    """A concrete place to capture from.

    ``offset_x``/``offset_y`` are the position of the surface within the *virtual*
    desktop coordinate space. This is what makes a frame's local coordinates
    convertible back to absolute screen coordinates for input - and getting that wrong
    is the classic cause of "it clicked the right thing on the wrong monitor", which
    this machine's side-by-side 1920+1920 topology makes a live risk rather than a
    theoretical one.
    """

    kind: SurfaceKind
    size: Size
    offset_x: int = 0
    offset_y: int = 0
    monitor_index: int = 0
    hwnd: int | None = None
    label: str = ""

    def contains_point(self, surface_x: int, surface_y: int) -> bool:
        return 0 <= surface_x < self.size.width and 0 <= surface_y < self.size.height

    def to_screen(self, p_x: int, p_y: int) -> tuple[int, int]:
        """Surface-local pixels -> absolute virtual-desktop coordinates."""
        return (p_x + self.offset_x, p_y + self.offset_y)

    def from_screen(self, s_x: int, s_y: int) -> tuple[int, int]:
        return (s_x - self.offset_x, s_y - self.offset_y)

    def rect_to_screen(self, rect: Rect) -> Rect:
        return rect.translate(self.offset_x, self.offset_y)


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured image plus everything needed to reason about *when* and *where*.

    ``content_hash`` hashes the pixel bytes only (not metadata), so two frames with
    identical pixels hash identically. The verifier, the change detector and the replay
    logic all depend on that property, and it is what lets a report bind a verdict to
    an exact image (guardrail G-VERD-03).
    """

    array: np.ndarray  # HxWx3 uint8, RGB order
    surface: Surface
    mono_ms: float
    wall_ms: float
    index: int
    content_hash: str = ""
    capture_ms: float = 0.0
    source: str = ""

    @property
    def size(self) -> Size:
        return self.surface.size

    @property
    def roi(self) -> Rect:
        """The frame's own rectangle in surface-local coordinates."""
        return Rect(x=0, y=0, width=self.array.shape[1], height=self.array.shape[0])

    def region(self, rect: Rect) -> np.ndarray:
        """Crop to ``rect`` (clamped). Returns a view where possible."""
        r = rect.clamp_to(self.roi)
        return self.array[r.y : r.bottom, r.x : r.right]

    def to_screen_point(self, surface_x: int, surface_y: int) -> tuple[int, int]:
        return self.surface.to_screen(surface_x, surface_y)


class CaptureHealth(StrEnum):
    """Liveness classification for a capture source.

    ``UNSUPPORTED`` is distinct from ``BLACK`` on purpose. Exclusive fullscreen is a
    display-driver decision we cannot code around; reporting it as "black" would send a
    user hunting for a Frame Forge bug that does not exist (stability criterion 7).
    """

    OK = "ok"
    BLACK = "black"
    FROZEN = "frozen"
    LOST = "lost"
    UNSUPPORTED = "unsupported"
    DEGRADED = "degraded"


@dataclass(slots=True)
class CaptureStatus:
    """Health snapshot from a capture source."""

    health: CaptureHealth
    detail: str = ""
    consecutive_failures: int = 0
    last_frame_mono_ms: float | None = None
    last_error: str | None = None
    fps_estimate: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.health in (CaptureHealth.OK, CaptureHealth.DEGRADED)


@runtime_checkable
class CapturePort(Protocol):
    """A source of frames for one surface.

    Lifecycle: ``open()`` -> repeated ``grab()`` -> ``close()``. ``grab()`` returns
    ``None`` when no new frame is available yet, which lets the director poll without
    blocking and without spinning a CPU core.
    """

    name: str

    def open(self, surface: Surface) -> None:
        """Begin capturing ``surface``. Raise on failure."""
        ...

    def grab(self) -> Frame | None:
        """Return the next available frame, or ``None`` if none is ready."""
        ...

    def status(self) -> CaptureStatus:
        """Current liveness/health."""
        ...

    def close(self) -> None:
        """Release OS resources. Must be idempotent."""
        ...


__all__ = [
    "CaptureHealth",
    "CapturePort",
    "CaptureStatus",
    "Frame",
    "Surface",
    "SurfaceKind",
]
