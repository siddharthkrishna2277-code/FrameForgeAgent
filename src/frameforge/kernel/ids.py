"""Run identifiers and content hashing.

Run ids are sortable by time and short enough to paste into a bug report. The
``ff-`` prefix makes Frame Forge run directories greppable when they sit next to real
game artifacts on a developer machine.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any


def new_run_id(now_iso: str) -> str:
    """Return a sortable run id: ``ff-<compact-timestamp>-<6 hex>``.

    ``now_iso`` is passed in rather than read from the clock so that the id is a pure
    function of its inputs and therefore trivially testable.
    """
    compact = now_iso.replace("-", "").replace(":", "").replace(".", "").replace("+0000", "")
    compact = compact.replace("T", "-").replace("Z", "")
    return f"ff-{compact[:15]}-{secrets.token_hex(3)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace.

    Used for hashing models and configs so a diff in a report means a real semantic
    change, not a key-ordering change.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def hash_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def short(digest: str, n: int = 12) -> str:
    return digest[:n]


__all__ = [
    "canonical_json",
    "hash_obj",
    "new_run_id",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "short",
]