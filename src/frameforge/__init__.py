"""Frame Forge - universal autonomous game operation and game QA for Windows.

The control loop is a deterministic state machine. AI, when configured, fills exactly
one slot in it: proposing the next *logical* action. It never emits input primitives,
never writes a verdict, and cannot widen its own permissions.

See docs/ROADMAP.md and docs/AI_GUARDRAILS.md.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]