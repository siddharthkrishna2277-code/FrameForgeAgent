"""Vision port - template matching, colour probes, and change detection.

Everything the system knows about "what is on screen" reduces to a handful of cheap,
deterministic, CPU-friendly measurements. This is deliberate: on an i5-8250U with
integrated graphics, OpenCV template matching over a handful of ROIs is milliseconds,
whereas anything heavier would eat the frame budget and force lower decision rates.

No model runs here. There is no local neural network in the critical path, by design
and by hardware constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from frameforge.ports.geometry import Point, Rect


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    """Result of matching one template within one region."""

    score: float
    rect: Rect | None
    template_size: tuple[int, int]
    method: str = "ccoeff_normed"

    @property
    def found(self) -> bool:
        return self.rect is not None

    @property
    def center(self) -> Point | None:
        return self.rect.center if self.rect else None


@dataclass(frozen=True, slots=True)
class TextMatch:
    """Result of a regex search over a cropped region's binary mask.

    Used by "is there a bright text-ish thing here" style probes that do not need a
    full OCR pass - notably detecting an active loading spinner.
    """

    score: float
    rect: Rect | None


@dataclass(frozen=True, slots=True)
class ChangeScore:
    """How different are two images, in [0, 1]."""

    score: float
    method: str
    detail: dict[str, float] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.score > 0.0


@dataclass(frozen=True, slots=True)
class ColorProbe:
    """A colour expectation, tolerant to compression and lighting drift.

    HSV rather than RGB: games apply subtle colour grading and gamma, so a raw RGB
    distance threshold is far too brittle for real captures.
    """

    hue: float
    saturation: float
    value: float
    hue_tol: float = 15.0
    sat_tol: float = 60.0
    val_tol: float = 60.0
    min_ratio: float = 0.05

    @classmethod
    def from_rgb(cls, r: int, g: int, b: int, **kw: float) -> ColorProbe:
        import colorsys

        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        return cls(h * 360.0, s * 255.0, v * 255.0, **kw)


@dataclass(frozen=True, slots=True)
class ColorProbeResult:
    ratio: float
    matched: bool
    mean_rgb: tuple[int, int, int] = (0, 0, 0)
    mean_hsv: tuple[float, float, float] = (0.0, 0.0, 0.0)


@runtime_checkable
class VisionPort(Protocol):
    """Deterministic image measurement. No learning, no model, no network."""

    name: str

    def to_gray(self, image: np.ndarray) -> np.ndarray:
        ...

    def encode_png(self, image: np.ndarray) -> bytes:
        """Serialise a region for evidence storage."""
        ...

    def match_template(
        self,
        haystack: np.ndarray,
        template: np.ndarray,
        threshold: float = 0.8,
        method: str = "ccoeff_normed",
    ) -> TemplateMatch:
        """Locate ``template`` inside ``haystack``.

        ``method`` defaults to normalised correlation, which is invariant to linear
        brightness changes - the property that makes a template survive a game's
        exposure slider.
        """
        ...

    def match_templates(
        self,
        haystack: np.ndarray,
        templates: list[np.ndarray],
        threshold: float = 0.8,
    ) -> list[TemplateMatch]:
        ...

    def diff(self, a: np.ndarray, b: np.ndarray) -> ChangeScore:
        """Fraction of differing pixels / mean absolute difference."""
        ...

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Structural similarity in [0, 1]. 1.0 means identical."""
        ...

    def probe_color(
        self, image: np.ndarray, probe: ColorProbe, rect: Rect | None = None
    ) -> ColorProbeResult:
        ...

    def region_activity(self, image: np.ndarray, rect: Rect | None = None) -> float:
        """0..1 measure of visual activity (edges/entropy).

        The cheapest possible "is something animating here" signal, used to wait out a
        loading screen without paying for OCR every frame.
        """
        ...

    def is_black(self, image: np.ndarray, threshold: float = 3.0) -> bool:
        """Detect a black frame (a classic symptom of unsupported capture modes)."""
        ...

    def downscale(self, image: np.ndarray, max_dimension: int) -> np.ndarray:
        """Shrink for AI packets or for fast diffing."""
        ...


__all__ = [
    "ChangeScore",
    "ColorProbe",
    "ColorProbeResult",
    "TemplateMatch",
    "TextMatch",
    "VisionPort",
]
