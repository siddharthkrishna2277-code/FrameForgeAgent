"""Frame assembler: pixels + window + OCR + anchors -> one immutable Observation.

This is the boundary where the world becomes something a planner can reason about, and
it is deliberately the *only* place that touches the frame. Everything downstream reads
the ``Observation``, never the raw pixels.

Three decisions worth stating:

* **Caching.** OCR costs 120-400 ms; template matching costs tens. Both are computed at
  most once per frame and shared by every consumer, which is what keeps the decision
  cycle inside its budget.
* **Observation is immutable and hashable-ish.** A plan recorded against observation N
  replays against observation N. Mutability here would break replay.
* **The planner sees a *summary*, not the frame.** :class:`PerceptionSummary` is the
  distilled form. That keeps Tier 0 deterministic and keeps the AI packet small
  (guardrail G-PER-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.perception.health import HealthReport, HealthVerdict
from frameforge.ports.capture import Frame
from frameforge.ports.geometry import Rect, Size
from frameforge.ports.ocr import OcrResult
from frameforge.ports.planner import AnchorObservation, PerceptionSummary
from frameforge.ports.vision import ColorProbe, VisionPort
from frameforge.ports.window import WindowInfo

if TYPE_CHECKING:  # pragma: no cover
    from frameforge.profiles.schema import GameProfile


@dataclass(frozen=True, slots=True)
class AnchorSpec:
    """A visual landmark declaration, resolved against a frame."""

    name: str
    kind: str                       # "template" | "text" | "color" | "activity"
    rect: Rect | None = None
    template: np.ndarray | None = None
    pattern: str = ""
    probe: ColorProbe | None = None
    threshold: float = 0.8
    #: Optional post-match narrowing, e.g. require the text to be inside a sub-region.
    required: bool = False


@dataclass(frozen=True, slots=True)
class AnchorResult:
    name: str
    present: bool
    score: float
    rect: Rect | None = None
    center: tuple[int, int] | None = None

    def to_observation(self, threshold: float) -> AnchorObservation:
        return AnchorObservation(
            name=self.name,
            score=round(self.score, 4),
            rect=self.rect.as_tuple() if self.rect else None,
            center=self.center,
            threshold=threshold,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """One immutable perception epoch."""

    frame: Frame
    window: WindowInfo | None
    ocr: OcrResult
    anchors: tuple[AnchorResult, ...]
    color_probes: tuple[tuple[str, bool, float], ...]
    health: HealthReport
    confidence: float
    mono_ms: float
    topology_epoch: int = 0
    notes: tuple[str, ...] = ()

    @property
    def frame_hash(self) -> str:
        return self.frame.content_hash

    @property
    def surface_size(self) -> Size:
        return self.frame.size

    @property
    def usable(self) -> bool:
        return self.health.verdict in (HealthVerdict.OK, HealthVerdict.WATCH)

    def anchor(self, name: str) -> AnchorResult | None:
        return next((a for a in self.anchors if a.name == name), None)

    def anchor_present(self, name: str) -> bool:
        a = self.anchor(name)
        return bool(a and a.present)

    def present_anchors(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.anchors if a.present)

    def probe(self, name: str) -> bool:
        return any(n == name and ok for n, ok, _ in self.color_probes)

    def probe_ratio(self, name: str) -> float:
        for n, _ok, ratio in self.color_probes:
            if n == name:
                return ratio
        return 0.0

    def to_summary(self, max_ocr: int = 20) -> PerceptionSummary:
        """Distil to what a planner is allowed to see."""
        return PerceptionSummary(
            mono_ms=self.mono_ms,
            frame_index=self.frame.index,
            frame_hash=self.frame.content_hash,
            surface_size=self.frame.size.as_tuple(),
            window_title=self.window.title if self.window else "",
            window_class=self.window.class_name if self.window else "",
            process_name=self.window.process_name if self.window else "",
            is_foreground=bool(self.window and self.window.is_foreground),
            anchors=tuple(
                a.to_observation(0.8) for a in self.anchors
            ),
            ocr_lines=tuple(
                (line.text, line.rect.as_tuple(), round(line.confidence, 3))
                for line in self.ocr.lines[:max_ocr]
            ),
            color_probes=self.color_probes,
            region_activity=float(self.health.metrics.get("activity", 0.0)),
            health=str(self.health.capture),
            confidence=round(self.confidence, 4),
            notes=self.notes,
        )


class FrameAssembler:
    """Builds Observations. Owns the per-frame caches."""

    def __init__(
        self,
        vision: VisionPort,
        *,
        clock: ClockPort | None = None,
        max_ocr_lines: int = 40,
    ) -> None:
        self._vision = vision
        self._clock = clock or SystemClock()
        self._max_ocr_lines = max_ocr_lines
        self._last_ocr: OcrResult | None = None
        self._last_ocr_frame: str | None = None
        self.assembled = 0
        self.ocr_calls = 0

    # ----------------------------------------------------------------- OCR cache

    def read_text(self, frame: Frame, ocr_port, *, force: bool = False) -> OcrResult:
        """OCR for this frame, cached.

        The cache is keyed on frame content hash, so two consumers in the same epoch pay
        once and a genuinely new frame always re-reads.
        """
        if not force and self._last_ocr_frame == frame.content_hash and self._last_ocr is not None:
            return self._last_ocr
        self.ocr_calls += 1
        result = ocr_port.read(frame.array)
        lines = result.lines[: self._max_ocr_lines]
        capped = OcrResult(
            lines=lines,
            engine=result.engine,
            mono_ms=result.mono_ms,
            width=result.width,
            height=result.height,
            error=result.error,
        )
        self._last_ocr = capped
        self._last_ocr_frame = frame.content_hash
        return capped

    def invalidate(self) -> None:
        self._last_ocr = None
        self._last_ocr_frame = None

    # ------------------------------------------------------------------- anchors

    def evaluate_anchors(
        self,
        frame: Frame,
        specs: list[AnchorSpec],
        ocr_result: OcrResult | None = None,
    ) -> tuple[AnchorResult, ...]:
        """Evaluate every anchor against one frame.

        Always returns a result for every spec, including below-threshold ones with their
        real score. Tier-0 policies want the *degree* of a match, not just presence.
        """
        out: list[AnchorResult] = []
        for spec in specs:
            out.append(self._evaluate_one(frame, spec, ocr_result))
        return tuple(out)

    def _evaluate_one(
        self, frame: Frame, spec: AnchorSpec, ocr_result: OcrResult | None
    ) -> AnchorResult:
        roi = spec.rect
        if roi is not None:
            roi = roi.clamp_to(frame.roi)
            region = frame.region(roi)
        else:
            region = frame.array

        if spec.kind == "template":
            if spec.template is None:
                return AnchorResult(spec.name, False, 0.0)
            match = self._vision.match_template(region, spec.template, spec.threshold)
            if match.rect is None:
                return AnchorResult(spec.name, False, match.score)
            # Translate match coords back to surface coords for a usable centre point.
            abs_rect = match.rect.translate(roi.x, roi.y) if roi is not None else match.rect
            return AnchorResult(spec.name, True, match.score, abs_rect, abs_rect.center.as_tuple())

        if spec.kind == "text":
            if ocr_result is None:
                return AnchorResult(spec.name, False, 0.0)
            line = ocr_result.find(spec.pattern, roi)
            if line is None:
                return AnchorResult(spec.name, False, 0.0)
            return AnchorResult(
                spec.name, True, line.confidence, line.rect, line.rect.center.as_tuple()
            )

        if spec.kind == "color":
            if spec.probe is None:
                return AnchorResult(spec.name, False, 0.0)
            result = self._vision.probe_color(region, spec.probe, None)
            return AnchorResult(
                spec.name,
                result.matched,
                result.ratio,
                roi,
                roi.center.as_tuple() if roi else None,
            )

        if spec.kind == "activity":
            activity = self._vision.region_activity(region, None)
            return AnchorResult(
                spec.name, activity > spec.threshold, activity, roi,
                roi.center.as_tuple() if roi else None,
            )

        return AnchorResult(spec.name, False, 0.0)

    # ------------------------------------------------------------------ assembly

    def assemble(
        self,
        frame: Frame,
        *,
        window: WindowInfo | None,
        ocr: OcrResult,
        anchors: tuple[AnchorResult, ...] = (),
        color_probes: tuple[tuple[str, bool, float], ...] = (),
        health: HealthReport | None = None,
        topology_epoch: int = 0,
        notes: tuple[str, ...] = (),
    ) -> Observation:
        """Assemble the epoch and compute a confidence score.

        Confidence is computed from *signals*, never self-reported by a planner
        (guardrail G-DEC-04). The weighting is intentionally blunt:

        * health dominates - unhealthy perception means nothing downstream is trustworthy;
        * OCR availability contributes, because text assertions become UNKNOWN without it;
        * anchor agreement contributes, because many strong anchors mean the state is
          unambiguous.
        """
        from frameforge.ports.capture import CaptureHealth
        from frameforge.perception.health import HealthReport, HealthVerdict

        if health is None:
            # No health report means the caller has not evaluated capture health yet;
            # treat that as nominal rather than inventing a verdict from the frame.
            health = HealthReport(verdict=HealthVerdict.OK, capture=CaptureHealth.OK)
        if frame is None:
            # An Observation without a frame is not a degraded observation, it is a bug in
            # the caller. Callers that legitimately have no frame (the verifier, after a
            # failed capture) already return UNKNOWN before reaching here. Fail loudly
            # rather than constructing a half-null observation that crashes later in a more
            # confusing place.
            msg = "assemble() requires a frame; handle a missing frame in the caller"
            raise ValueError(msg)

        score = 1.0
        notes_out = list(notes)

        match health.verdict:
            case HealthVerdict.OK:
                pass
            case HealthVerdict.WATCH:
                score -= 0.15
            case HealthVerdict.PAUSE:
                score -= 0.45
            case HealthVerdict.FATAL:
                score = 0.0

        if ocr.error:
            score -= 0.10
            notes_out.append(f"ocr_unavailable: {ocr.error}")

        if anchors:
            strong = sum(1 for a in anchors if a.present)
            if strong:
                score += min(0.05, 0.01 * strong)

        if window is None:
            score -= 0.30
            notes_out.append("no_window_identity")

        self.assembled += 1
        return Observation(
            frame=frame,
            window=window,
            ocr=ocr,
            anchors=anchors,
            color_probes=color_probes,
            health=health,
            confidence=max(0.0, min(1.0, score)),
            mono_ms=self._clock.monotonic_ms(),
            topology_epoch=topology_epoch,
            notes=tuple(notes_out),
        )


__all__ = ["AnchorResult", "AnchorSpec", "FrameAssembler", "Observation"]
