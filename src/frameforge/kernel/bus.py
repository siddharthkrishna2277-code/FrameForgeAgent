"""Append-only event log (guardrail layer L6).

``EventLog`` owns the run's audit trail. Three properties matter:

1. **Append-only.** ``append`` is the only mutation method; there is no update or
   delete. Corrections append a new event with ``supersedes`` set (G-AUD-02).
2. **Redacted on the way out.** Every payload passes through :class:`Redactor` before
   it is serialised, so secrets cannot reach disk even if a caller passes them in
   (G-PER-03).
3. **Crash-safe.** The file is opened per-append and flushed, because the most likely
   time to lose the tail of an audit trail is exactly when it matters most: a crash
   during a run.

The in-memory list is retained for the run summary; it is bounded by
``max_memory_events`` so a long run cannot exhaust 12 GB of RAM.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.events import Event, EventKind
from frameforge.kernel.errors import Redacted


class Redactor:
    """Strips secrets from anything bound for disk or an AI provider.

    Deliberately pattern-based and deliberately blunt: a false positive costs a redacted
    OCR line in a log, a false negative leaks a credential. The patterns cover the
    shapes that actually appear in this domain - API keys, bearer tokens, JWTs, PEM
    blocks, and card-number-shaped digit runs.

    Extensible via ``extra_patterns`` for site- or profile-specific secrets (guardrail
    G-PER-03).
    """

    #: (name, compiled pattern). Keep these deliberately broad.
    DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
        ("pem_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        ("bearer", r"(?i)\b(bearer|authorization:\s*bearer)\s+[A-Za-z0-9._\-]{16,}"),
        ("openai_key", r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
        ("google_key", r"\bAIza[0-9A-Za-z_-]{30,}\b"),
        ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
        ("generic_api_key", r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})"),
        ("card_number", r"\b(?:\d[ -]?){13,19}\b"),
    )

    PLACEHOLDER = "[REDACTED]"

    def __init__(self, extra_patterns: tuple[tuple[str, str], ...] = (), enabled: bool = True) -> None:
        import re

        self._enabled = enabled
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for name, pattern in (*self.DEFAULT_PATTERNS, *extra_patterns):
            self._patterns.append((name, re.compile(pattern)))

    def scrub(self, text: str) -> str:
        """Return ``text`` with every known secret shape replaced."""
        if not self._enabled or not text:
            return text
        out = text
        for _name, pattern in self._patterns:
            if pattern.search(out):
                out = pattern.sub(self.PLACEHOLDER, out)
        return out

    def scrub_obj(self, obj: Any, _depth: int = 0) -> Any:
        """Recursively scrub strings inside dicts/lists/tuples.

        Depth-limited so a pathological object graph cannot stall the event loop while
        we are trying to write an audit record.
        """
        if not self._enabled:
            return obj
        if _depth > 12:
            return "[TRUNCATED_DEPTH]"
        match obj:
            case str():
                return self.scrub(obj)
            case dict():
                return {str(k): self.scrub_obj(v, _depth + 1) for k, v in obj.items()}
            case list() | tuple():
                return [self.scrub_obj(v, _depth + 1) for v in obj]
            case int() | float() | bool() | None:
                return obj
            case _:
                # Don't serialise arbitrary objects into the audit trail; str() is enough
                # for diagnosis and cannot silently capture a large object graph.
                return self.scrub(str(obj))


class EventLog:
    """Append-only, redacting, crash-safe JSONL event sink.

    Not a broker: it is the record. Subscription lives in
    :mod:`frameforge.kernel.bus`, which forwards from here so that anything subscribed
    sees exactly the same redacted events that reach disk.
    """

    def __init__(
        self,
        path: Path | None,
        run_id: str,
        *,
        clock: ClockPort | None = None,
        redactor: Redactor | None = None,
        max_memory_events: int = 20_000,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._run_id = run_id
        self._clock = clock or SystemClock()
        self._redactor = redactor or Redactor()
        self._max_memory = max_memory_events
        self._events: list[Event] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._redaction_hits = 0

        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ properties

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def events(self) -> list[Event]:
        """In-memory copy of retained events (bounded)."""
        return list(self._events)

    @property
    def redaction_hits(self) -> int:
        """How many payloads were altered. Non-zero is worth reporting."""
        return self._redaction_hits

    # --------------------------------------------------------------------- writing

    def append(
        self,
        kind: EventKind,
        component: str,
        summary: str,
        data: dict[str, Any] | None = None,
        *,
        supersedes: int | None = None,
        **fields: Any,
    ) -> Event:
        """Append one event. Thread-safe.

        Extra keyword arguments are folded into ``data``. They exist because the natural
        call shape is ``append(kind, who, "what happened", detail="...")``, and passing
        those as a dict literal at every one of the hundreds of call sites invites exactly
        the mistake that was made here: a keyword argument silently becomes a TypeError.
        """
        if fields:
            merged = dict(data or {})
            merged.update(fields)
            data = merged
        with self._lock:
            scrubbed = self._redactor.scrub_obj(data or {})
            if scrubbed != (data or {}):
                self._redaction_hits += 1
            self._seq += 1
            event = Event(
                seq=self._seq,
                kind=kind,
                mono_ms=self._clock.monotonic_ms(),
                wall_ms=self._clock.wall_ms(),
                component=component,
                run_id=self._run_id,
                summary=summary[:400],
                data=scrubbed,
                supersedes=supersedes,
            )
            if len(self._events) < self._max_memory:
                self._events.append(event)
            self._write(event)
            return event

    def _write(self, event: Event) -> None:
        if self._path is None:
            return
        try:
            line = event.model_dump_json()
        except Exception as exc:  # pragma: no cover - defensive
            raise Redacted(f"event not serialisable: {exc}") from exc
        # Append + flush per event: a crash mid-run must not cost us the audit trail.
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    # --------------------------------------------------------------------- reading

    def read_all(self) -> list[Event]:
        """Read the persisted log back. Used by replay and by the report builder."""
        if self._path is None or not self._path.exists():
            return list(self._events)
        events: list[Event] = []
        with self._path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(Event.model_validate_json(line))
                except ValueError:
                    # A truncated final line after a hard crash is expected; skip it
                    # rather than failing the whole report.
                    continue
        return events

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[str(event.kind)] = counts.get(str(event.kind), 0) + 1
        return counts

    def close(self) -> None:
        """Present for symmetry with the context-manager protocol."""

    def __enter__(self) -> EventLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def to_jsonl(self) -> str:
        """Serialise retained events. Used by tests and by ``frameforge simulate``."""
        return "".join(e.model_dump_json() + "\n" for e in self._events)


def load_events(path: Path) -> list[Event]:
    """Read a run's ``events.jsonl``. Tolerates a truncated tail."""
    log = EventLog(path, run_id=path.parent.name)
    return log.read_all()


def event_to_dict(event: Event) -> dict[str, Any]:
    return event.model_dump(mode="json")


def dump_events_jsonl(events: list[Event], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event_to_dict(event), sort_keys=True) + "\n")


__all__ = ["EventLog", "Redactor", "dump_events_jsonl", "event_to_dict", "load_events"]