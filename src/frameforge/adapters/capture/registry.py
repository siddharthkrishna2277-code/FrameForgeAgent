"""Capture backend selection with graceful degradation.

The whole point of putting DXGI behind a port is exercised here: a misconfigured or
unavailable backend produces a warning event and a working MSS source, never an
exception that ends a run before it starts.
"""

from __future__ import annotations

from typing import Callable

from frameforge.kernel.events import EventKind
from frameforge.ports.capture import CapturePort, Surface
from frameforge.ports.window import WindowPort


def build_capture_source(
    backend: str,
    window: WindowPort,
    surface: Surface,
    *,
    on_warning: Callable[[str], None] | None = None,
) -> CapturePort:
    """Return an opened capture source for ``backend``, degrading to mss on failure."""

    def warn(msg: str) -> None:
        if on_warning:
            on_warning(msg)

    if backend == "dxgi":
        try:
            from frameforge.adapters.capture.dxcam_capture import DxgiCaptureSource

            src: CapturePort = DxgiCaptureSource()
            src.open(surface)
            return src
        except Exception as exc:
            warn(f"dxgi capture unavailable ({exc}); falling back to mss")
            warn(EventKind.CAPTURE_UNSUPPORTED.value)

    from frameforge.adapters.capture.mss_capture import MssCaptureSource

    fallback = MssCaptureSource()
    fallback.open(surface)
    return fallback


__all__ = ["build_capture_source"]
