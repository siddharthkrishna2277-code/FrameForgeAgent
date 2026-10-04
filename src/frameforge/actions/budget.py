"""Bounded context for the planner and the AI packet.

Adopted from a study of how mature agent systems bound what enters a model's context
(docs/RESEARCH_CODEX.md §5). Three rules there, and this module exists to enforce them
mechanically rather than by convention:

1. **No unbounded items.** Anything injected into context has a hard cap.
2. **No oversized items.** A single fragment may not exceed a token budget.
3. **Report what was dropped.** A silent truncation is worse than a large one, because the
   planner cannot tell that its view of the screen was incomplete.

The value here is the third rule as much as the first two. A perception packet that quietly
lost its OCR lines looks identical to one where the screen genuinely had no text, and that
ambiguity produces confident wrong decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Rough characters-per-token. Deliberately conservative; a wrong estimate in the safe
#: direction (fewer tokens claimed) costs context, not correctness.
CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class Budget:
    """A hard ceiling on context, with accounting for what did not fit."""

    max_tokens: int = 8000
    max_item_tokens: int = 2000
    used_tokens: int = 0
    items: list[tuple[str, int]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    oversized: list[str] = field(default_factory=list)

    def estimate(self, text: str) -> int:
        return max(1, len(text) // CHARS_PER_TOKEN)

    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)

    def add(self, name: str, text: str, *, required: bool = False) -> bool:
        """Add a fragment. Returns whether it fit.

        Required fragments are never dropped - the objective and the target identity, for
        instance. If they do not fit, that is a configuration error the caller must see,
        so ``oversized`` records it rather than silently trimming.
        """
        cost = self.estimate(text)
        if cost > self.max_item_tokens:
            if required:
                self.oversized.append(f"{name} ({cost}t > {self.max_item_tokens}t)")
                self.used_tokens += cost
                return False
            self.oversized.append(f"{name} ({cost}t)")
            self.dropped.append(name)
            return False

        if cost > self.remaining() and not required:
            self.dropped.append(f"{name} ({cost}t, {self.remaining()}t left)")
            return False

        self.items.append((name, cost))
        self.used_tokens += cost
        return True

    def add_all(self, fragments: dict[str, str], required: tuple[str, ...] = ()) -> None:
        """Add fragments in a deterministic order: required first, then by insertion.

        Determinism matters more than optimality here - the same screen must produce the
        same packet, or a recorded run cannot be replayed.
        """
        for name in required:
            if name in fragments:
                self.add(name, fragments[name], required=True)
        for name, text in fragments.items():
            if name not in required:
                self.add(name, text)

    def report(self) -> dict[str, object]:
        return {
            "max_tokens": self.max_tokens,
            "used_tokens": self.used_tokens,
            "utilisation": round(self.used_tokens / max(1, self.max_tokens), 3),
            "items": [{"name": n, "tokens": t} for n, t in self.items],
            "dropped": list(self.dropped),
            "oversized": list(self.oversized),
        }

    def describe(self) -> str:
        return (f"{self.used_tokens}/{self.max_tokens} tokens, "
                f"{len(self.items)} item(s), {len(self.dropped)} dropped")


def build_packet_budget(settings_max_ocr: int = 25, *, max_tokens: int = 8000) -> Budget:
    """Budget sized for a perception packet.

    Deliberately modest. The planner's job is to choose the next action from a *distilled*
    view; giving it the whole frame teaches it to depend on detail it cannot rely on being
    there next epoch.
    """
    return Budget(max_tokens=max_tokens, max_item_tokens=2000)
