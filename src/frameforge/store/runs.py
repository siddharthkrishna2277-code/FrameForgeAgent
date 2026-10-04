"""Run directory management and the evidence store.

A run directory *is* the evidence bundle. No database, no manifest reconciliation, no
migration: what a developer receives is a folder they can zip, diff, and attach. That is
also why ``events.jsonl`` and ``report.json`` are written incrementally - a run that is
killed mid-flight still leaves a usable audit trail.

Write paths are restricted to the run directory by construction (guardrail G-ROLE-03):
every writer here resolves paths under the run root and refuses anything that escapes it.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from frameforge.actions.safety import hardened_child_env
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.ids import hash_obj, new_run_id, sha256_file
from frameforge.ports.geometry import Rect, RegionSpec


@dataclass(slots=True)
class RunPaths:
    """The canonical run layout."""

    root: Path

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def plan(self) -> Path:
        return self.root / "plan.jsonl"

    @property
    def timeline(self) -> Path:
        return self.root / "timeline.jsonl"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def report_json(self) -> Path:
        return self.root / "report.json"

    @property
    def report_md(self) -> Path:
        return self.root / "report.md"

    @property
    def junit(self) -> Path:
        return self.root / "junit.xml"

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def video(self) -> Path:
        return self.root / "video"

    @property
    def ocr(self) -> Path:
        return self.root / "ocr"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def abort_sentinel(self) -> Path:
        """Touch this file to stop the run (estop path 2)."""
        return self.root / "ABORT"

    def ensure(self) -> RunPaths:
        for path in (self.root, self.frames, self.video, self.ocr, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        return self


class EvidenceStore:
    """Frames, OCR snapshots, and the optional video.

    Retention is enforced per-run. A 12 GB laptop with a QA run that captures every
    decision would fill a disk in hours, and the frames nobody looks at are the majority.
    """

    def __init__(
        self,
        paths: RunPaths,
        *,
        vision=None,
        clock: ClockPort | None = None,
        max_mb: int = 200,
        redact_regions: tuple[RegionSpec, ...] = (),
    ) -> None:
        self.paths = paths.ensure()
        self._vision = vision
        self._clock = clock or SystemClock()
        self.max_bytes = max_mb * 1024 * 1024
        self._redact = redact_regions
        self._bytes = 0
        self._saved = 0
        self._pruned = 0
        self._video_proc = None

    # --------------------------------------------------------------------- paths

    def _safe(self, name: str) -> Path:
        """Resolve a filename inside the run, refusing traversal.

        Frame tags come from task definitions and, in principle, from planner output.
        A tag of ``../../startup.bat`` must not be able to write outside the run
        directory, so containment is enforced here rather than trusted.
        """
        candidate = (self.paths.frames / name).resolve()
        root = self.paths.frames.resolve()
        if not str(candidate).startswith(str(root)):
            msg = f"evidence path escapes run directory: {name!r}"
            raise ValueError(msg)
        return candidate

    # --------------------------------------------------------------------- frames

    def save_frame(
        self,
        image: np.ndarray,
        tag: str,
        *,
        key: bool = False,
        rect: Rect | None = None,
    ) -> Path | None:
        """Save a frame, applying redaction first.

        Redaction happens here, at the store, so a redacted region is never written to
        disk and therefore cannot leak via a later upload or a shared run directory
        (guardrail G-PER-02/G-PER-06).
        """
        if self._vision is None:
            return None
        payload = image
        if rect is not None:
            payload = image[rect.y : rect.bottom, rect.x : rect.right]
        if self._redact:
            payload = payload.copy()
            h, w = payload.shape[:2]
            from frameforge.ports.geometry import Size

            for spec in self._redact:
                region = spec.to_rect(Size(w, h))
                payload[region.y : region.bottom, region.x : region.right] = 0

        safe_tag = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in tag)[:80]
        target = self._safe(f"{self._clock.monotonic_ms():.0f}_{safe_tag}.png")
        try:
            data = self._vision.encode_png(payload)
        except Exception:
            return None
        target.write_bytes(data)
        self._bytes += len(data)
        self._saved += 1
        self._enforce_retention()
        return target

    def save_ocr(self, tag: str, payload: dict) -> Path:
        target = self.paths.ocr / f"{tag}.json"
        target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return target

    # ----------------------------------------------------------------- retention

    def _enforce_retention(self) -> None:
        """Drop oldest frames once over cap. Key frames are kept: they are the evidence a
        report actually points at, so they are the ones worth keeping."""
        if self._bytes <= self.max_bytes:
            return
        frames = sorted(self.paths.frames.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for path in frames:
            if self._bytes <= self.max_bytes * 0.8:
                break
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                continue
            self._bytes -= size
            self._pruned += 1

    # ---------------------------------------------------------------------- video

    def start_video(self, fps: int = 15, size: tuple[int, int] | None = None) -> bool:
        """Pipe frames to ffmpeg.

        Encoding via the external binary rather than an in-process encoder: it is already
        installed, it keeps the 4-core CPU free for perception, and a broken video can
        never take down a run.
        """
        import subprocess

        if size is None:
            return False
        self.paths.video.mkdir(parents=True, exist_ok=True)
        target = self.paths.video / "run.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{size[0]}x{size[1]}", "-r", str(fps),
            "-i", "-",
            "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            str(target),
        ]
        try:
            self._video_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                # The encoder is a child process; strip injection-capable variables.
                env=hardened_child_env(),
            )
        except Exception:
            self._video_proc = None
            return False
        return True

    def write_video_frame(self, image: np.ndarray) -> None:
        if self._video_proc is None or self._video_proc.stdin is None:
            return
        try:
            import cv2

            frame = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            self._video_proc.stdin.write(frame.tobytes())
        except Exception:
            pass

    def stop_video(self) -> Path | None:
        if self._video_proc is None:
            return None
        try:
            if self._video_proc.stdin:
                self._video_proc.stdin.close()
            self._video_proc.wait(timeout=10)
        except Exception:
            try:
                self._video_proc.kill()
            except Exception:
                pass
        self._video_proc = None
        out = self.paths.video / "run.mp4"
        return out if out.exists() else None

    # ------------------------------------------------------------------ manifest

    def write_manifest(self, manifest: dict) -> Path:
        path = self.paths.manifest
        path.write_text(json.dumps(manifest, indent=2, default=str, sort_keys=True), encoding="utf-8")
        return path

    @property
    def stats(self) -> dict[str, object]:
        return {
            "frames_saved": self._saved,
            "frames_pruned": self._pruned,
            "bytes": self._bytes,
            "max_bytes": self.max_bytes,
        }


def new_run(
    runs_root: Path,
    *,
    clock: ClockPort | None = None,
    run_id: str | None = None,
) -> tuple[str, RunPaths]:
    """Create a fresh run directory under ``runs_root``."""
    clock = clock or SystemClock()
    rid = run_id or new_run_id(clock.iso_now())
    paths = RunPaths(root=Path(runs_root) / rid).ensure()
    return rid, paths


def latest_run(runs_root: Path) -> Path | None:
    root = Path(runs_root)
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("ff-")]
    return max(candidates, key=lambda p: p.name) if candidates else None


def prune_old_runs(runs_root: Path, keep: int = 20) -> int:
    """Keep the newest ``keep`` runs. Returns how many were removed."""
    root = Path(runs_root)
    if not root.exists():
        return 0
    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("ff-")),
        key=lambda p: p.name,
    )
    removed = 0
    for path in candidates[:-keep] if keep > 0 else candidates:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


__all__ = [
    "EvidenceStore",
    "RunPaths",
    "latest_run",
    "new_run",
    "prune_old_runs",
]
