"""RapidOCR + onnxruntime adapter - optional fallback for stylized game fonts.

Used when WinRT OCR underperforms on a game's custom typeface. onnxruntime CPU is
roughly 15 MB, which is affordable, but it is an *extra*: the system is designed to work
without it, and a machine with neither backend degrades to vision-only mode rather than
failing.
"""

from __future__ import annotations

import numpy as np

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.adapters.ocr.winrt_ocr import WinRtOcr
from frameforge.ports.geometry import Rect
from frameforge.ports.ocr import OcrCapabilities, OcrLine, OcrResult


class RapidOcr:
    """Text recognition via RapidOCR."""

    name = "rapidocr"

    def __init__(self, clock: ClockPort | None = None) -> None:
        self._clock = clock or SystemClock()
        self._engine = None
        self._error: str | None = None
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._engine is not None

    def read(self, image: np.ndarray) -> OcrResult:
        if self._engine is None:
            return OcrResult(engine=self.name, error=self._error or "rapidocr unavailable")
        t0 = self._clock.monotonic_ms()
        try:
            # RapidOCR expects BGR.
            result, _elapsed = self._engine(image[:, :, ::-1])
            lines: list[OcrLine] = []
            for box, text, score in result or []:
                xs = [int(p[0]) for p in box]
                ys = [int(p[1]) for p in box]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                lines.append(
                    OcrLine(
                        text=str(text),
                        rect=Rect(max(0, x0), max(0, y0), max(1, x1 - x0), max(1, y1 - y0)),
                        confidence=float(score),
                    )
                )
            h, w = image.shape[:2]
            return OcrResult(
                lines=tuple(lines),
                engine=self.name,
                mono_ms=self._clock.monotonic_ms() - t0,
                width=w,
                height=h,
            )
        except Exception as exc:
            return OcrResult(
                engine=self.name,
                error=f"{type(exc).__name__}: {exc}",
                mono_ms=self._clock.monotonic_ms() - t0,
            )

    def capabilities(self) -> OcrCapabilities:
        return OcrCapabilities(
            available=self.available,
            engines=(self.name,) if self.available else (),
            primary=self.name if self.available else "none",
            notes=() if self.available else (self._error or "rapidocr not installed",),
        )


class NullOcr:
    """No OCR backend. Reports honestly so the report can say *why*."""

    name = "null_ocr"

    def __init__(self, reason: str = "no OCR backend available") -> None:
        self._reason = reason

    def read(self, image: np.ndarray) -> OcrResult:
        return OcrResult(engine=self.name, error=self._reason)

    def capabilities(self) -> OcrCapabilities:
        return OcrCapabilities(
            available=False,
            notes=("vision-only mode: text assertions evaluate to UNKNOWN", self._reason),
        )


def build_ocr(primary: str = "auto", clock: ClockPort | None = None):
    """Pick an OCR backend, degrading honestly.

    ``auto`` prefers WinRT (zero install, OS-tuned) and falls back to RapidOCR. An
    explicit ``null`` forces vision-only mode, which is useful for reproducing a
    customer's environment with OCR disabled.
    """
    if primary == "null":
        return NullOcr("disabled by configuration")
    candidates = []
    if primary in ("auto", "winrt"):
        candidates.append(lambda: WinRtOcr(clock=clock))
    if primary in ("auto", "rapidocr"):
        candidates.append(lambda: RapidOcr(clock=clock))
    if primary not in ("auto", "winrt", "rapidocr", "null"):
        return NullOcr(f"unknown ocr backend {primary!r}")
    errors: list[str] = []
    for factory in candidates:
        try:
            engine = factory()
            caps = engine.capabilities()
            if caps.available:
                return engine
            errors.extend(caps.notes)
        except Exception as exc:
            errors.append(f"{factory}: {type(exc).__name__}: {exc}")
    # Report *why* every candidate failed. A bare "unavailable" sends the operator
    # hunting; the specific reason is what tells them whether to install an extra, fix a
    # PowerShell policy, or accept vision-only mode.
    return NullOcr("; ".join(errors) or "no configured OCR backend could be initialised")


__all__ = ["NullOcr", "RapidOcr", "WinRtOcr", "build_ocr"]
