"""Capture and display health monitoring.

Continuous, cheap, and deliberately paranoid. Its job is to answer one question before
the director does anything else: *is it safe and meaningful to reason about what we just
captured?*

Detected conditions:

* **black frames** - almost always unsupported capture (exclusive fullscreen) or a
  minimised window. Reported as BLACK, with UNSUPPORTED raised separately when the
  window is fullscreen, because those need different operator responses.
* **frozen frames** - identical content hash across many samples while the window claims
  to be animating. A frozen capture makes every "screen changed" postcondition pass
  vacuously, which would silently corrupt QA verdicts. This is the single most important
  check here.
* **lost capture** - repeated adapter failures.
* **display/topology change** - any change to the monitor signature invalidates every
  cached ROI and every surface-to-screen mapping, so it forces re-acquisition.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.ports.capture import CaptureHealth, CaptureStatus, Frame
from frameforge.ports.vision import VisionPort
from frameforge.ports.window import DisplayTopology, WindowPort


class HealthVerdict(StrEnum):
    """What the director should do about current health."""

    OK = "ok"
    WATCH = "watch"          # slightly degraded, keep going but log
    PAUSE = "pause"          # stop, re-acquire, then resume
    FATAL = "fatal"          # cannot continue meaningfully


@dataclass(slots=True)
class HealthReport:
    verdict: HealthVerdict
    capture: CaptureHealth
    detail: str = ""
    changed: bool = False
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.verdict in (HealthVerdict.PAUSE, HealthVerdict.FATAL)

    def describe(self) -> str:
        base = f"{self.verdict}: capture={self.capture}"
        return f"{base} ({self.detail})" if self.detail else base


@dataclass(slots=True)
class HealthPolicy:
    """Thresholds. Defaults chosen for 1080p at ~10 Hz."""

    #: Identical content hash for this many consecutive frames -> frozen.
    frozen_after_frames: int = 6
    #: Fraction of the frame that must be non-black for a healthy capture.
    min_mean_luma: float = 4.0
    #: Region activity below this for N frames is "static" (used for loading waits).
    idle_activity: float = 0.004
    #: Consecutive identical frames before declaring the game frozen.
    frozen_window: int = 6
    #: Occluded fraction above which capture is considered unusable. Partial coverage is the
    #: dangerous case, so this is deliberately low: a window 25% covered is already enough
    #: to make OCR and template matching unreliable.
    occlusion_fail_fraction: float = 0.20
    #: Occlusion above this is merely worth reporting.
    occlusion_warn_fraction: float = 0.01


class DisplayHealthMonitor:
    """Tracks capture liveness and display topology across the run."""

    def __init__(
        self,
        window: WindowPort,
        vision: VisionPort,
        *,
        policy: HealthPolicy | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._window = window
        self._vision = vision
        self.policy = policy or HealthPolicy()
        self._clock = clock or SystemClock()
        self._hashes: deque[str] = deque(maxlen=64)
        self._activity: deque[float] = deque(maxlen=32)
        self._signature: str | None = None
        self._topology: DisplayTopology | None = None
        self._last_luma = 255.0
        self._epoch = 0
        self._last_occlusion = None
        self.occlusion_events = 0
        self.popup_events = 0

    # ------------------------------------------------------------------- topology

    def refresh_topology(self) -> tuple[DisplayTopology, bool]:
        """Read monitors. Returns ``(topology, changed)``.

        A change invalidates cached ROIs and surface offsets, so the director re-acquires.
        """
        topology = self._window.monitors()
        changed = self._signature is not None and topology.signature != self._signature
        if self._signature is None:
            self._signature = topology.signature
        elif changed:
            self._signature = topology.signature
            self._epoch += 1
        self._topology = topology
        return topology, changed

    @property
    def topology(self) -> DisplayTopology | None:
        return self._topology

    @property
    def epoch(self) -> int:
        """Increments on each topology change. Cached observations carry this value."""
        return self._epoch

    # --------------------------------------------------------------------- checks

    def observe(self, frame: Frame | None, status: CaptureStatus | None = None) -> HealthReport:
        """Evaluate health for one epoch."""
        metrics: dict[str, float] = {}

        if frame is None:
            health = status.health if status else CaptureHealth.LOST
            return HealthReport(
                verdict=HealthVerdict.PAUSE,
                capture=health,
                detail=status.detail if status else "no frame available",
                metrics=metrics,
            )

        # Black-frame detection.
        gray_mean = float(self._vision.to_gray(frame.array).mean())
        self._last_luma = gray_mean
        metrics["mean_luma"] = gray_mean
        if gray_mean < self.policy.min_mean_luma:
            return HealthReport(
                verdict=HealthVerdict.FATAL,
                capture=CaptureHealth.BLACK,
                detail=f"frame is black (mean luma {gray_mean:.2f}); capture mode unsupported?",
                metrics=metrics,
            )

        # Frozen detection: same pixels, many times running.
        self._hashes.append(frame.content_hash)
        identical = sum(1 for h in self._hashes if h == frame.content_hash)
        metrics["identical_frames"] = float(identical)
        if len(self._hashes) >= self.policy.frozen_after_frames and identical >= self.policy.frozen_after_frames:
            # A genuinely static screen is legitimate (a paused menu). Distinguish by
            # asking whether the window claims to be foregrounded and un-minimised; if it
            # is, report WATCH rather than FATAL, and let the director's own postcondition
            # decide whether "nothing changed" is expected.
            return HealthReport(
                verdict=HealthVerdict.WATCH,
                capture=CaptureHealth.FROZEN,
                detail=f"{identical} identical frames; screen may simply be static",
                metrics=metrics,
            )

        # Activity, for loading-screen waits.
        activity = self._vision.region_activity(frame.array)
        self._activity.append(activity)
        metrics["activity"] = activity

        if status is not None and status.health is CaptureHealth.LOST:
            return HealthReport(
                verdict=HealthVerdict.PAUSE,
                capture=status.health,
                detail=status.detail or status.last_error or "capture lost",
                metrics=metrics,
            )

        return HealthReport(verdict=HealthVerdict.OK, capture=CaptureHealth.OK, metrics=metrics)

    def is_static(self) -> bool:
        """True when recent frames have shown no activity.

        Used by loading waits: "wait until it stops animating" rather than
        "sleep for N seconds and assume success".
        """
        if len(self._activity) < 3:
            return False
        recent = list(self._activity)[-3:]
        return all(a < self.policy.idle_activity for a in recent)

    def check_occlusion(self, hwnd: int, origin, size) -> HealthReport:
        """Report when something other than the target is on screen in its rect.

        Capture is a screen grab, not a window render. If a notification, tooltip or other
        window covers the target, every subsequent frame is a mix of two applications -
        and because the pixels *change*, change-detection reads it as activity. That
        silently invalidates postconditions, which is the worst possible failure mode for a
        QA tool because it looks like the application is busy.
        """
        from frameforge.ports.geometry import Size
        from frameforge.ports.window import OcclusionInfo

        info = self._window.occlusion(hwnd, origin, size)
        self._last_occlusion = info
        if info.target_popup and not info.occluded:
            # The target's own popup is on screen: a menu, tooltip or flyout. It is not a
            # foreign occluder, but it does cover the pixels the run is trying to read, so
            # the honest report is "a popup is in the way", not "the landmark is absent".
            self.popup_events += 1
            return HealthReport(
                verdict=HealthVerdict.WATCH,
                capture=CaptureHealth.OK,
                detail=("a popup belonging to the target is on screen "
                        f"({', '.join(info.covering_classes) or 'unknown class'}); "
                        "dismiss it before the next perception step"),
                metrics={"target_popup": 1.0},
            )
        if not info.occluded:
            return HealthReport(verdict=HealthVerdict.OK, capture=CaptureHealth.OK,
                                metrics={"occluded_fraction": 0.0})
        self.occlusion_events += 1
        detail = (
            f"{info.occluded_fraction:.0%} of the target is covered by "
            f"{list(info.covering_titles or info.covering_classes)}"
        )
        metrics = {"occluded_fraction": info.occluded_fraction}
        if info.occluded_fraction >= self.policy.occlusion_fail_fraction:
            return HealthReport(verdict=HealthVerdict.PAUSE, capture=CaptureHealth.DEGRADED,
                                detail=detail, metrics=metrics)
        return HealthReport(verdict=HealthVerdict.WATCH, capture=CaptureHealth.OK,
                            detail=detail, metrics=metrics)

    @property
    def last_occlusion(self):
        return self._last_occlusion

    @property
    def mean_activity(self) -> float:
        if not self._activity:
            return 0.0
        return sum(self._activity) / len(self._activity)


__all__ = ["DisplayHealthMonitor", "HealthPolicy", "HealthReport", "HealthVerdict"]
