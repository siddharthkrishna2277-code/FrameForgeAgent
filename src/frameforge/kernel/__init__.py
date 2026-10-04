"""Kernel: event spine, clock, ids, state machine, errors.

Nothing in this package may know about a specific game, a screen coordinate, or a key
binding. That invariant is enforced by tests/unit/test_no_hardcoded_games.py and is
guardrail G-DEV-01.
"""

from __future__ import annotations

__all__: list[str] = []