"""Geometry primitives shared across perception, actions and profiles.

Deliberately frozen dataclasses rather than pydantic models: these are hot-path value
types constructed thousands of times per run, and ``Point(3, 4)`` is what the rest of
the codebase should read like. Validation lives in ``__post_init__`` where it is still
unavoidable.

Regions in *profiles* are normalised by default so coordinates survive a resolution
change. ``RegionSpec`` additionally carries an explicit ``unit``, so a profile author
must state ``norm`` or ``px``; there is no heuristic in the engine, because an ambiguity
that reaches the input layer silently is an ambiguity that eventually clicks the wrong
thing on the wrong monitor.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp01(v: float) -> float:
    return min(max(v, 0.0), 1.0)


@dataclass(frozen=True, slots=True)
class Point:
    """A point in capture-surface pixels (or absolute screen pixels for input)."""

    x: int
    y: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", int(self.x))
        object.__setattr__(self, "y", int(self.y))

    def as_tuple(self) -> tuple[int, int]:
        return (self.x, self.y)

    def offset(self, dx: int, dy: int) -> Point:
        return Point(self.x + dx, self.y + dy)

    def distance_to(self, other: Point) -> float:
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


@dataclass(frozen=True, slots=True)
class Size:
    """A width/height pair."""

    width: int
    height: int

    def __post_init__(self) -> None:
        w, h = int(self.width), int(self.height)
        if w < 0 or h < 0:
            msg = f"negative size: {w}x{h}"
            raise ValueError(msg)
        object.__setattr__(self, "width", w)
        object.__setattr__(self, "height", h)

    def as_tuple(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def contains(self, p: Point) -> bool:
        return 0 <= p.x < self.width and 0 <= p.y < self.height

    def scaled(self, factor: float) -> Size:
        return Size(max(1, int(self.width * factor)), max(1, int(self.height * factor)))


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle in capture-surface pixels."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        x, y, w, h = int(self.x), int(self.y), int(self.width), int(self.height)
        if w <= 0 or h <= 0:
            msg = f"degenerate rect ({x}, {y}, {w}, {h})"
            raise ValueError(msg)
        if x < 0 or y < 0:
            msg = f"negative origin ({x}, {y})"
            raise ValueError(msg)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "width", w)
        object.__setattr__(self, "height", h)

    @classmethod
    def full(cls, surface: Size) -> Rect:
        return cls(0, 0, surface.width, surface.height)

    @classmethod
    def from_norm(cls, nx: float, ny: float, nw: float, nh: float, surface: Size) -> Rect:
        """Build from fractions of ``surface``, clamped to stay valid."""
        nx, ny = _clamp01(nx), _clamp01(ny)
        # Clamp the *size* to whatever is left after the origin, so a profile ROI written
        # for a wider surface still yields a valid rect rather than an out-of-bounds slice.
        nw = min(_clamp01(nw), 1.0 - nx)
        nh = min(_clamp01(nh), 1.0 - ny)
        return cls(
            int(nx * surface.width),
            int(ny * surface.height),
            max(1, int(nw * surface.width)),
            max(1, int(nh * surface.height)),
        )

    @classmethod
    def from_px(cls, x: int, y: int, w: int, h: int, surface: Size | None = None) -> Rect:
        r = cls(max(0, int(x)), max(0, int(y)), max(1, int(w)), max(1, int(h)))
        return r.clamp_to(surface) if surface is not None else r

    @property
    def size(self) -> Size:
        return Size(self.width, self.height)

    @property
    def center(self) -> Point:
        return Point(self.x + self.width // 2, self.y + self.height // 2)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.right and self.y <= p.y < self.bottom

    def intersects(self, other: Rect) -> bool:
        return not (
            self.right <= other.x
            or other.right <= self.x
            or self.bottom <= other.y
            or other.bottom <= self.y
        )

    def intersection(self, other: Rect) -> Rect | None:
        if not self.intersects(other):
            return None
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.right, other.right), min(self.bottom, other.bottom)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def clamp_to(self, surface: Size) -> Rect:
        """Shrink (never translate) so the rect fits inside ``surface``.

        Shrinking keeps a profile ROI anchored to the edge its author meant: "the left
        quarter" stays the left quarter on a narrower surface.
        """
        w = min(self.width, surface.width)
        h = min(self.height, surface.height)
        return Rect(
            min(self.x, max(0, surface.width - w)),
            min(self.y, max(0, surface.height - h)),
            max(1, w),
            max(1, h),
        )

    def translate(self, dx: int, dy: int) -> Rect:
        return Rect(max(0, self.x + dx), max(0, self.y + dy), self.width, self.height)

    def rescale_to(self, new_surface: Size, old_surface: Size) -> Rect:
        """Map a rect expressed in ``old_surface`` onto ``new_surface``.

        Aspect ratios must agree within 2%: stretching a HUD ROI and letterboxing it
        are different intents, so we refuse rather than guess.
        """
        if old_surface.width <= 0 or old_surface.height <= 0:
            return self
        if abs(new_surface.aspect - old_surface.aspect) > 0.02:
            msg = (
                f"aspect mismatch rescaling {self.as_tuple()} from "
                f"{old_surface.width}x{old_surface.height} to "
                f"{new_surface.width}x{new_surface.height}"
            )
            raise ValueError(msg)
        sx = new_surface.width / old_surface.width
        sy = new_surface.height / old_surface.height
        return Rect(
            int(self.x * sx),
            int(self.y * sy),
            max(1, int(self.width * sx)),
            max(1, int(self.height * sy)),
        ).clamp_to(new_surface)

    def relative_to(self, surface: Size) -> tuple[float, float, float, float]:
        """Normalised ``(x, y, w, h)`` fractions of ``surface``."""
        return (
            self.x / max(1, surface.width),
            self.y / max(1, surface.height),
            self.width / max(1, surface.width),
            self.height / max(1, surface.height),
        )

    def union(self, other: Rect) -> Rect:
        x0, y0 = min(self.x, other.x), min(self.y, other.y)
        x1, y1 = max(self.right, other.right), max(self.bottom, other.bottom)
        return Rect(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True, slots=True)
class RegionSpec:
    """A rectangle as written in a profile, with an explicit unit."""

    x: float
    y: float
    width: float
    height: float
    unit: str = "norm"

    def __post_init__(self) -> None:
        if self.unit not in ("norm", "px"):
            msg = f"unit must be 'norm' or 'px', got {self.unit!r}"
            raise ValueError(msg)
        if self.width <= 0 or self.height <= 0:
            msg = f"degenerate region {self}"
            raise ValueError(msg)
        if self.unit == "norm":
            for name in ("x", "y", "width", "height"):
                v = getattr(self, name)
                if not (0.0 <= v <= 1.0):
                    msg = f"norm region {name}={v} out of [0,1]"
                    raise ValueError(msg)
        elif self.x < 0 or self.y < 0:
            msg = "px region origin must be >= 0"
            raise ValueError(msg)

    def to_rect(self, surface: Size) -> Rect:
        if self.unit == "norm":
            return Rect.from_norm(self.x, self.y, self.width, self.height, surface)
        return Rect(
            int(self.x), int(self.y), max(1, int(self.width)), max(1, int(self.height))
        ).clamp_to(surface)


__all__ = ["Point", "Rect", "RegionSpec", "Size"]
