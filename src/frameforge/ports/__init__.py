"""Ports: the interfaces every Windows-specific capability is written against.

This package contains *no* Windows code. It contains the contracts. Two reasons that
matters:

1. Every test in the project runs headless. Core logic is tested against fakes, so a
   regression shows up on a machine with no display and no game.
2. A future remote Executor (see docs/ROADMAP.md §14) is a transport change, not a
   redesign: all Windows specifics already sit behind these interfaces.

Each port has at least one real adapter and one fake. That is stability criterion 25.
"""

from __future__ import annotations

__all__: list[str] = []