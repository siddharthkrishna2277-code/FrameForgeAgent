"""DXGI Desktop Duplication capture - optional accelerated path.

Desktop Duplication gives a GPU-backed texture, which is lower latency than BitBlt and
avoids a full-screen GDI copy. On an i5-8250U with Intel UHD 620 the difference is real
but not dramatic at our decision rates.

Promotion is a configuration flag (``capture.backend: dxcam``). Every known failure
mode - missing extra, driver refusal, black output on exclusive fullscreen - degrades to
MSS with an explicit event rather than raising, because capture is not optional.
"""

from __future__ import annotations

import numpy as np

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import CapabilityUnavailable, CaptureHealthError
from frameforge.ports.capture import CaptureHealth, CaptureStatus, Frame, Surface


class DxgiCaptureSource:
    """Desktop Duplication capture via ``dxcam``."""

    name = "dxcam"

    def __init__(self, clock: ClockPort | None = None, output_idx: int = 0) -> None:
        self._clock = clock or SystemClock()
        self._output_idx = output_idx
        self._cam = None
        self._surface: Surface | None = None
        self._index = 0
        self._last_error: str | None = None
        self._failures = 0

    def open(self, surface: Surface) -> None:
        try:
            import dxcam
        except ImportError as exc:
            msg = "dxcam is not installed; install the 'dxgi' extra or use capture.backend: mss"
            raise CapabilityUnavailable(msg) from exc

        try:
            self._cam = dxcam.create(output_idx=self._output_idx)
        except Exception as exc:
            msg = f"dxcam.create failed: {exc}"
            raise CaptureHealthError(msg) from exc
        if self._cam is None:
            msg = "dxcam.create returned None (driver refused Desktop Duplication)"
            raise CaptureHealthError(msg)
        self._surface = surface
        self._index = 0
        self._failures = 0

    def grab(self) -> Frame | None:
        if self._cam is None or self._surface is None:
            return None
        t0 = self._clock.monotonic_ms()
        try:
            # region=(left, top, right, bottom)
            bbox = (
                self._surface.offset_x,
                self._surface.offset_y,
                self._surface.offset_x + self._surface.size.width,
                self._surface.offset_y + self._surface.size.height,
            )
            arr = self._cam.grab(region=bbox)
        except Exception as exc:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None
        t1 = self._clock.monotonic_ms()
        if arr is None:
            self._failures += 1
            self._last_error = "dxcam returned no frame"
            return None

        rgb = np.ascontiguousarray(arr[:, :, :3])
        self._index += 1
        self._failures = 0
        return Frame(
            array=rgb,
            surface=self._surface,
            mono_ms=t1,
            wall_ms=self._clock.wall_ms(),
            index=self._index,
            content_hash=f"dxgi{self._index}",
            capture_ms=t1 - t0,
            source=self.name,
        )

    def status(self) -> CaptureStatus:
        if self._cam is None:
            return CaptureStatus(health=CaptureHealth.LOST, detail="not open")
        if self._failures:
            return CaptureStatus(
                health=CaptureHealth.LOST,
                detail="dxcam failures",
                consecutive_failures=self._failures,
                last_error=self._last_error,
            )
        return CaptureStatus(health=CaptureHealth.OK)

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.release()
            except Exception:  # pragma: no cover
                pass
        self._cam = None


__all__ = ["DxgiCaptureSource"]
