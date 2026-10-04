"""MSS (BitBlt) capture adapter - the default.

Chosen as the default over DXGI on purpose. DXGI Desktop Duplication is faster, but it
is sensitive to driver quirks, and P0-P3 must never be blocked on one. ``mss`` is a
~1 MB dependency that works across RDP, VM display drivers and every configuration we
care about, so the accelerated path becomes a promotion rather than a prerequisite.

Colour order: ``mss`` returns BGRA. Frame Forge's canonical order is RGB, so this
adapter converts once, at the boundary, and nothing downstream ever thinks about it.
That single conversion is why no later module has a colour-order bug.
"""

from __future__ import annotations

import numpy as np

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import CaptureHealthError
from frameforge.ports.capture import CaptureHealth, CaptureStatus, Frame, Surface, SurfaceKind


class MssCaptureSource:
    """Screen capture via ``mss``."""

    name = "mss"

    def __init__(self, clock: ClockPort | None = None) -> None:
        self._clock = clock or SystemClock()
        self._sct = None
        self._surface: Surface | None = None
        self._index = 0
        self._failures = 0
        self._last_error: str | None = None
        self._last_frame_ms: float | None = None
        self._last_hash = ""
        self._frame_times: list[float] = []

    def open(self, surface: Surface) -> None:
        try:
            import mss
        except ImportError as exc:  # pragma: no cover - dependency is in the floor
            msg = "mss is required for capture; install the core dependencies"
            raise CaptureHealthError(msg) from exc

        # mss 10.x renamed the factory to ``MSS``; ``mss.mss()`` still works but emits a
        # DeprecationWarning, and this project turns warnings into errors in tests. Prefer
        # the new name and fall back for older 9.x installs.
        try:
            self._sct = mss.MSS()
        except AttributeError:
            self._sct = mss.mss()
        self._surface = surface
        self._index = 0
        self._failures = 0
        self._last_error = None
        self._frame_times.clear()

    def _monitor_rect(self, surface: Surface) -> dict[str, int]:
        """Absolute virtual-desktop rectangle for this surface.

        ``mss`` addresses the virtual desktop, so a secondary monitor needs its real
        left offset. On this machine DISPLAY1 sits at x=1920, and getting this wrong is
        precisely how a "correct" click lands on the wrong monitor.
        """
        return {
            "left": surface.offset_x,
            "top": surface.offset_y,
            "width": surface.size.width,
            "height": surface.size.height,
        }

    def grab(self) -> Frame | None:
        if self._sct is None or self._surface is None:
            return None
        t0 = self._clock.monotonic_ms()
        try:
            raw = self._sct.grab(self._monitor_rect(self._surface))
        except Exception as exc:
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None

        # BGRA -> RGB, and drop alpha. Done here, once, so the rest of the codebase has
        # exactly one colour convention.
        bgra = np.asarray(raw, dtype=np.uint8)
        rgb = bgra[:, :, 2::-1].copy()

        h, w = rgb.shape[:2]
        expected = self._surface.size
        if (w, h) != expected.as_tuple():
            # A resolution change mid-run. The health monitor owns the response; we
            # surface the mismatch rather than silently rescaling, because scaling would
            # invalidate every cached ROI without announcing it.
            self._failures += 1
            self._last_error = f"surface size changed: got {w}x{h}, expected {expected.width}x{expected.height}"
            return None

        t1 = self._clock.monotonic_ms()
        self._index += 1
        self._failures = 0
        self._last_frame_ms = t1
        self._frame_times.append(t1)
        if len(self._frame_times) > 30:
            self._frame_times.pop(0)

        content_hash = _fast_hash(rgb)
        self._last_hash = content_hash
        return Frame(
            array=rgb,
            surface=self._surface,
            mono_ms=t1,
            wall_ms=self._clock.wall_ms(),
            index=self._index,
            content_hash=content_hash,
            capture_ms=t1 - t0,
            source=self.name,
        )

    def status(self) -> CaptureStatus:
        if self._sct is None:
            return CaptureStatus(health=CaptureHealth.LOST, detail="not open")
        if self._failures:
            return CaptureStatus(
                health=CaptureHealth.LOST,
                detail="repeated capture failures",
                consecutive_failures=self._failures,
                last_error=self._last_error,
            )
        times = self._frame_times
        fps = 0.0
        if len(times) >= 2:
            span = times[-1] - times[0]
            fps = round((len(times) - 1) / (span / 1000.0), 2) if span > 0 else 0.0
        return CaptureStatus(
            health=CaptureHealth.OK, last_frame_mono_ms=self._last_frame_ms, fps_estimate=fps
        )

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # pragma: no cover - best effort
                pass
        self._sct = None


def _fast_hash(rgb: np.ndarray) -> str:
    """Cheap stable content hash.

    Hashes a strided sample plus shape rather than every byte: at 1920x1080 a full
    hash costs real milliseconds, and a sample is more than enough to distinguish
    "this frame changed" from "this frame is the same", which is all we use it for.
    The sample stride is fixed so the hash is stable across runs.
    """
    import hashlib

    h = hashlib.blake2b(digest_size=12)
    h.update(f"{rgb.shape[0]}x{rgb.shape[1]}x{rgb.shape[2]}".encode())
    h.update(np.ascontiguousarray(rgb[::8, ::8]).tobytes())
    return h.hexdigest()


__all__ = ["MssCaptureSource"]
