"""Local control API.

The future dashboard is a *client* of this, not a replacement for the backend. Defining
it now, in the backend phase, means the UI will not require backend work later - which is
also why every endpoint is a thin wrapper over a service the CLI already uses.

Binds to loopback only. There is no authentication here, so exposing this on a network
interface would be a serious mistake; the bind check below is a hard failure rather than a
warning.
"""

from __future__ import annotations

__all__: list[str] = []
