"""Named coordinate spaces and the conversions between them.

Every mouse coordinate in Frame Forge is a bare ``int``, which is precisely how a
surface-local point gets sent as a virtual-desktop point and lands a click in the wrong
window. This module makes the spaces *distinct types*, so mixing them is a type error at
construction rather than a wrong click at runtime.

The spaces, and what each one means:

``SurfacePx``   relative to the target window's client area. What profiles and scenarios
                author. Resolution- and monitor-independent.
``ScreenPx``   virtual-desktop **physical** pixels. Origin may be negative. What Windows
                cursor APIs take.
``SendInputPx`` the 0..65535 normalised space ``MOUSEEVENTF_ABSOLUTE`` requires, across
                the whole virtual desktop (not the primary monitor).

There is deliberately no "image pixel" type: capture is surface-local, so image
coordinates *are* surface coordinates. Adding a fourth name for the same numbers is how
they get confused.
"""

from __future__ import annotations

from dataclasses import dataclass

from frameforge.ports.geometry import Point, Rect, Size


@dataclass(frozen=True, slots=True)
class SurfacePx:
    """A point in target-client-area pixels."""

    x: int
    y: int

    @classmethod
    def of(cls, p: Point) -> SurfacePx:
        return cls(int(p.x), int(p.y))

    def to_point(self) -> Point:
        return Point(self.x, self.y)


@dataclass(frozen=True, slots=True)
class ScreenPx:
    """A point in virtual-desktop physical pixels. Origin is top-left of the virtual
    screen and may be negative when a monitor sits left of or above the primary."""

    x: int
    y: int

    @classmethod
    def of(cls, p: Point) -> ScreenPx:
        return cls(int(p.x), int(p.y))

    def to_point(self) -> Point:
        return Point(self.x, self.y)


@dataclass(frozen=True, slots=True)
class SendInputPx:
    """Normalised absolute coordinates in [0, 65535] spanning the virtual desktop."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class VirtualDesktop:
    """The virtual screen rectangle, in physical pixels.

    Read once from ``GetSystemMetrics`` and passed explicitly, so no conversion can quietly
    assume the primary monitor is the whole desktop.
    """

    x: int
    y: int
    width: int
    height: int

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.width, self.height)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, p: ScreenPx) -> bool:
        return self.x <= p.x < self.right and self.y <= p.y < self.bottom

    def to_send_input(self, p: ScreenPx) -> SendInputPx:
        """Physical virtual-desktop pixels -> SendInput's normalised space.

        The denominator is the virtual desktop's **full extent**, not the primary monitor's
        size, and the numerator is relative to the virtual origin rather than absolute.
        Both mistakes are common and both put the cursor somewhere plausible-looking and
        wrong, which is worse than an obvious failure.
        """
        if self.width <= 0 or self.height <= 0:
            msg = f"degenerate virtual desktop {self.width}x{self.height}"
            raise ValueError(msg)
        # Denominator is the full extent, and the numerator is relative to the virtual
        # origin. Using (width - 1) here made the far edge land exactly on 65535 but skewed
        # everything in between - a 1920/3840 split produced 32776 rather than 32767, i.e. a
        # ~9 px bias at the centre of a two-monitor desktop. Dividing by the full extent
        # removes the bias; the last pixel then maps to 65534, which is one quantisation step
        # and not a misdirected click.
        nx = int(round((p.x - self.x) * 65535 / self.width))
        ny = int(round((p.y - self.y) * 65535 / self.height))
        return SendInputPx(max(0, min(65535, nx)), max(0, min(65535, ny)))

    def describe(self) -> str:
        return (f"virtual desktop origin=({self.x},{self.y}) "
                f"size={self.width}x{self.height}")


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """One display, in physical pixels."""

    index: int
    device_name: str
    rect: Rect
    work_area: Rect | None = None
    dpi: int = 96
    orientation: str = "landscape"
    is_primary: bool = False
    scale_percent: int = 100

    @property
    def scale(self) -> float:
        return self.dpi / 96.0 if self.dpi else 1.0

    def contains(self, p: ScreenPx) -> bool:
        return (self.rect.x <= p.x < self.rect.right
                and self.rect.y <= p.y < self.rect.bottom)

    def describe(self) -> str:
        return (f"monitor[{self.index}] {self.device_name} {self.rect.as_tuple()} "
                f"dpi={self.dpi} ({self.scale_percent}%) "
                f"{'primary' if self.is_primary else ''}").strip()


def surface_to_screen(point: SurfacePx, client_origin: tuple[int, int]) -> ScreenPx:
    """Surface-local -> virtual-desktop physical, via the window's client origin.

    The offset must come from ``ClientToScreen``, not from a monitor's origin: a window
    can sit anywhere, including partly off-screen.
    """
    return ScreenPx(point.x + int(client_origin[0]), point.y + int(client_origin[1]))


def screen_to_surface(point: ScreenPx, client_origin: tuple[int, int]) -> SurfacePx:
    return SurfacePx(point.x - int(client_origin[0]), point.y - int(client_origin[1]))


def assert_in_bounds(point: ScreenPx, desktop: VirtualDesktop, *, what: str = "point") -> None:
    """Reject a point outside the virtual desktop.

    A point outside every monitor cannot be clicked, and attempting it would normalise into
    a *valid* 0..65535 value and land on a monitor the author never meant. Clamping or
    wrapping silently is how a click ends up somewhere plausible and wrong.
    """
    if not desktop.contains(point):
        msg = (f"{what} {point.x},{point.y} is outside the "
               f"{desktop.describe()}")
        raise ValueError(msg)


def assert_in_rect(point: ScreenPx, rect: Rect, *, what: str = "point") -> None:
    if not (rect.x <= point.x < rect.right and rect.y <= point.y < rect.bottom):
        msg = (f"{what} {point.x},{point.y} is outside "
               f"{rect.as_tuple()}")
        raise ValueError(msg)


__all__ = [
    "MonitorInfo",
    "ScreenPx",
    "SendInputPx",
    "SurfacePx",
    "VirtualDesktop",
    "assert_in_bounds",
    "assert_in_rect",
    "screen_to_surface",
    "surface_to_screen",
]
