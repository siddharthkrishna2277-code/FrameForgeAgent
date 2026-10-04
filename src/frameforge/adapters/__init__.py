"""Concrete implementations of the ports.

Windows-specific code lives here and nowhere else. Nothing in ``kernel``, ``actions``,
``perception``, ``planning``, ``tasks`` or ``qa`` imports a Win32 symbol directly, which
is what keeps the engine testable on a machine with no display.

Optional adapters (``dxgi``, ``rapidocr``) import their dependency lazily and raise
``CapabilityUnavailable`` when absent, so a missing extra degrades capability rather
than crashing the process.
"""

from __future__ import annotations

__all__: list[str] = []
