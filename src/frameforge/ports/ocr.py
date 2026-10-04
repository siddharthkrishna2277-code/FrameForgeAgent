"""OCR port.

Primary implementation is ``Windows.Media.Ocr`` (WinRT), verified working offline on
the development machine with en-US and en-GB installed. That removes what would
otherwise be the single largest dependency risk in the whole project: no Tesseract
binary to install, no model download, and no PATH plumbing.

RapidOCR+onnxruntime is the optional fallback for stylized game fonts where WinRT
underperforms. A ``NullOcr`` exists so that a machine with neither backend degrades to
"vision-only" mode instead of crashing.

Every result is cached per frame epoch by the assembler, because OCR is the most
expensive perception step (120-400 ms full-frame) and re-running it inside a decision
loop would halve the achievable decision rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from frameforge.ports.geometry import Rect


@dataclass(frozen=True, slots=True)
class OcrLine:
    """One line of recognised text with its location and confidence."""

    text: str
    rect: Rect
    confidence: float = 1.0

    def matches(self, pattern: str) -> bool:
        import re

        return re.search(pattern, self.text, re.IGNORECASE | re.DOTALL) is not None


@dataclass(frozen=True, slots=True)
class OcrResult:
    """All recognised text in a surface."""

    lines: tuple[OcrLine, ...] = ()
    engine: str = "none"
    mono_ms: float = 0.0
    width: int = 0
    height: int = 0
    error: str | None = None
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def find(self, pattern: str, rect: Rect | None = None) -> OcrLine | None:
        """First line matching ``pattern``, optionally restricted to a region."""
        for line in self.lines:
            if rect is not None and not rect.intersects(line.rect):
                continue
            if line.matches(pattern):
                return line
        return None

    def find_all(self, pattern: str, rect: Rect | None = None) -> list[OcrLine]:
        out = []
        for line in self.lines:
            if rect is not None and not rect.intersects(line.rect):
                continue
            if line.matches(pattern):
                out.append(line)
        return out

    def contains(self, pattern: str, rect: Rect | None = None) -> bool:
        return self.find(pattern, rect) is not None

    def in_region(self, rect: Rect) -> list[OcrLine]:
        return [line for line in self.lines if rect.intersects(line.rect)]


@dataclass(frozen=True, slots=True)
class OcrCapabilities:
    available: bool
    engines: tuple[str, ...] = ()
    primary: str = "none"
    languages: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@runtime_checkable
class OcrPort(Protocol):
    """Text extraction from a frame."""

    name: str

    def read(self, image: np.ndarray) -> OcrResult:
        """Recognise text.

        Must not raise for an ordinary recognition failure; return an ``OcrResult`` with
        ``error`` set. Raising is reserved for programmer error.
        """
        ...

    def capabilities(self) -> OcrCapabilities:
        ...


__all__ = ["OcrCapabilities", "OcrLine", "OcrPort", "OcrResult"]
