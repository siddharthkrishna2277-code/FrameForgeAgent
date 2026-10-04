"""Postconditions and verification - the deterministic half of the whole system.

Every action gets a postcondition. Nothing is ever assumed to have worked.

Three properties make this trustworthy for QA:

1. **UNKNOWN is a real answer.** If perception cannot evaluate a condition, the result is
   UNKNOWN. There is no code path that coerces UNKNOWN to PASS (guardrail G-VERD-02), and
   no "assume passed on timeout" anywhere in this file.
2. **Every verdict binds to evidence.** A verdict carries the frame hash and timestamp it
   came from, so a report can point a developer at the exact image (G-VERD-03).
3. **No AI in the import graph.** Nothing in this module imports a planner or an AI
   adapter. That is the property TEST-VERD-01 asserts statically, and it is what stops
   a model from ever influencing a pass/fail result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.events import Disposition
from frameforge.perception.assembler import Observation
from frameforge.ports.geometry import Rect
from frameforge.ports.vision import VisionPort


@dataclass(frozen=True, slots=True)
class Verdict:
    """The result of evaluating one condition.

    ``evidence`` points at the exact observation: frame hash, index, and timestamp. A
    verdict without evidence cannot be serialised into a report.
    """

    disposition: Disposition
    condition: str
    detail: str = ""
    confidence: float = 0.0
    frame_hash: str = ""
    frame_index: int = 0
    mono_ms: float = 0.0
    expected: str = ""
    actual: str = ""

    @property
    def ok(self) -> bool:
        return self.disposition is Disposition.PASS

    @property
    def unknown(self) -> bool:
        return self.disposition is Disposition.UNKNOWN

    @property
    def failed(self) -> bool:
        return self.disposition is Disposition.FAIL

    def describe(self) -> str:
        return f"{self.disposition.upper()} {self.condition}" + (f" - {self.detail}" if self.detail else "")


@runtime_checkable
class Condition(Protocol):
    """A checkable condition. Implemented by the dataclasses below."""

    name: str

    def evaluate(
        self,
        before: Observation | None,
        after: Observation,
        vision: VisionPort,
    ) -> Verdict: ...


def _verdict(
    obs: Observation | None,
    disposition: Disposition,
    condition: str,
    *,
    detail: str = "",
    expected: str = "",
    actual: str = "",
) -> Verdict:
    """Build a verdict, tolerating a missing observation.

    A missing observation always yields UNKNOWN. It must never yield PASS: "we could not
    look" is not "it was fine", and a verifier that crashes on a null frame would turn a
    perception outage into an unhandled error mid-run.
    """
    if obs is None:
        return Verdict(
            disposition=Disposition.UNKNOWN,
            condition=condition,
            detail=detail or "no observation available",
            confidence=0.0,
            expected=expected,
            actual=actual,
        )
    return Verdict(
        disposition=disposition,
        condition=condition,
        detail=detail,
        confidence=obs.confidence,
        frame_hash=obs.frame_hash,
        frame_index=obs.frame.index,
        mono_ms=obs.mono_ms,
        expected=expected,
        actual=actual,
    )


# --------------------------------------------------------------------- conditions


@dataclass(frozen=True, slots=True)
class ScreenChanged(Condition):
    """Something visibly changed in the ROI (or anywhere).

    The default "did my action do anything" check. ``min_score`` absorbs the noise floor
    from lossy scaling so a static screen does not read as changed.
    """

    name: str = "screen_changed"
    rect: Rect | None = None
    min_score: float = 0.002

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if before is None:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail="no baseline observation to compare against")
        a = before.frame.region(self.rect) if self.rect else before.frame.array
        b = after.frame.region(self.rect) if self.rect else after.frame.array
        if a.size == 0 or b.size == 0:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="empty region")
        score = vision.diff(a, b).score
        if score >= self.min_score:
            return _verdict(after, Disposition.PASS, self.name,
                            detail=f"change score {score:.4f}", expected=f">={self.min_score}",
                            actual=f"{score:.4f}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail="screen did not change", expected=f">={self.min_score}",
                        actual=f"{score:.4f}")


@dataclass(frozen=True, slots=True)
class NoChange(Condition):
    """Nothing changed for a while - the negative assertion.

    Used to wait out a loading screen ("it should stop animating") or to detect a hang
    ("the game did nothing for 3 seconds").
    """

    name: str = "no_change"
    rect: Rect | None = None
    max_score: float = 0.002

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if before is None:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="no baseline")
        a = before.frame.region(self.rect) if self.rect else before.frame.array
        b = after.frame.region(self.rect) if self.rect else after.frame.array
        score = vision.diff(a, b).score
        if score <= self.max_score:
            return _verdict(after, Disposition.PASS, self.name,
                            actual=f"{score:.4f}", expected=f"<={self.max_score}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"screen changed unexpectedly (score {score:.4f})",
                        actual=f"{score:.4f}")


@dataclass(frozen=True, slots=True)
class AnchorVisible(Condition):
    name: str = "anchor_visible"
    anchor: str = ""
    rect: Rect | None = None

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        result = after.anchor(self.anchor)
        if result is None:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"anchor {self.anchor!r} was never evaluated")
        if result.present:
            return _verdict(after, Disposition.PASS, self.name,
                            detail=f"{self.anchor} score={result.score:.3f}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"{self.anchor} not visible (score={result.score:.3f})",
                        expected="present", actual=f"score={result.score:.3f}")


@dataclass(frozen=True, slots=True)
class AnchorAbsent(Condition):
    name: str = "anchor_absent"
    anchor: str = ""

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        result = after.anchor(self.anchor)
        if result is None:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"anchor {self.anchor!r} was never evaluated")
        if not result.present:
            return _verdict(after, Disposition.PASS, self.name)
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"{self.anchor} still visible (score={result.score:.3f})")


@dataclass(frozen=True, slots=True)
class TextMatches(Condition):
    name: str = "text_matches"
    pattern: str = ""
    rect: Rect | None = None

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if after.ocr.error:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"OCR unavailable: {after.ocr.error}")
        line = after.ocr.find(self.pattern, self.rect)
        if line is not None:
            return _verdict(after, Disposition.PASS, self.name, detail=f"matched {line.text!r}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"no text matching {self.pattern!r}",
                        expected=self.pattern, actual=after.ocr.text[:120])


@dataclass(frozen=True, slots=True)
class TextAbsent(Condition):
    name: str = "text_absent"
    pattern: str = ""
    rect: Rect | None = None

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if after.ocr.error:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"OCR unavailable: {after.ocr.error}")
        if after.ocr.find(self.pattern, self.rect) is None:
            return _verdict(after, Disposition.PASS, self.name)
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"text {self.pattern!r} still present")


@dataclass(frozen=True, slots=True)
class RegionMatches(Condition):
    """Golden-image comparison against a reference template."""

    name: str = "region_matches"
    reference: np.ndarray | None = None
    rect: Rect | None = None
    threshold: float = 0.90

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if self.reference is None:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="no reference image")
        region = after.frame.region(self.rect) if self.rect else after.frame.array
        if region.size == 0:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="empty region")
        similarity = vision.similarity(region, self.reference)
        if similarity >= self.threshold:
            return _verdict(after, Disposition.PASS, self.name,
                            detail=f"similarity {similarity:.4f}", actual=f"{similarity:.4f}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"similarity {similarity:.4f} below {self.threshold}",
                        expected=f">={self.threshold}", actual=f"{similarity:.4f}")


@dataclass(frozen=True, slots=True)
class ColorProbeMatches(Condition):
    name: str = "color_probe"
    probe: str = ""
    expected: bool = True

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        ratio = after.probe_ratio(self.probe)
        if ratio == 0.0 and not after.probe(self.probe):
            # A probe that was never evaluated and did not match.
            actual = after.probe(self.probe)
            if actual != self.expected:
                return _verdict(after, Disposition.FAIL, self.name,
                                detail=f"{self.probe} = {actual}, expected {self.expected}")
        actual = after.probe(self.probe)
        if actual is self.expected:
            return _verdict(after, Disposition.PASS, self.name,
                            detail=f"{self.probe} ratio={ratio:.3f}")
        return _verdict(after, Disposition.FAIL, self.name,
                        detail=f"{self.probe} = {actual} (ratio={ratio:.3f}), expected {self.expected}",
                        expected=str(self.expected), actual=str(actual))


@dataclass(slots=True)
class WindowProperty(Condition):
    """Window title/class/size/foreground assertions.

    Note the ``UNKNOWN`` path: if there is no window identity at all, we cannot say the
    property holds, so we say UNKNOWN rather than PASS.
    """

    name: str = "window_property"
    property: str = "title"
    expected: str = ""
    regex: bool = False

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        w = after.window
        if w is None:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="no window identity")
        if self.property == "title":
            actual = w.title
        elif self.property == "class_name":
            actual = w.class_name
        elif self.property == "process_name":
            actual = w.process_name
        elif self.property == "foreground":
            actual = "true" if w.is_foreground else "false"
            return _verdict(after, Disposition.PASS if w.is_foreground else Disposition.FAIL,
                            self.name, detail=f"foreground={w.is_foreground}")
        elif self.property == "minimized":
            return _verdict(after, Disposition.PASS if w.is_minimized else Disposition.FAIL,
                            self.name, detail=f"minimized={w.is_minimized}")
        elif self.property == "size":
            if w.client_rect is None:
                return _verdict(after, Disposition.UNKNOWN, self.name, detail="no client rect")
            actual = f"{w.client_rect.width}x{w.client_rect.height}"
        else:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"unknown window property {self.property!r}")

        if self.regex:
            if re.search(self.expected, actual, re.IGNORECASE):
                return _verdict(after, Disposition.PASS, self.name, actual=actual)
            return _verdict(after, Disposition.FAIL, self.name,
                            expected=f"~{self.expected}", actual=actual)
        if actual == self.expected:
            return _verdict(after, Disposition.PASS, self.name, actual=actual)
        return _verdict(after, Disposition.FAIL, self.name,
                        expected=self.expected, actual=actual)


@dataclass(slots=True)
class SignalEquals(Condition):
    """Assert non-visual state from an *authorized* developer signal.

    This is the sanctioned seam between Frame Forge and the game under test: a QA build
    that writes "HP=37" to a log or exposes a status endpoint lets a scenario assert on
    state that is not on screen. It is C3-adjacent (it reads an external source) so the
    signal must be declared in the profile with an explicit source.

    Never satisfied by assumption: if no reader is configured, the result is UNKNOWN.
    """

    name: str = "signal_equals"
    key: str = ""
    expected: str = ""
    numeric_range: tuple[float, float] | None = None
    reader: object = None

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if self.reader is None:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail="no signal reader configured for this scenario")
        try:
            value = self.reader.read(self.key)  # type: ignore[attr-defined]
        except Exception as exc:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"signal read failed: {type(exc).__name__}")
        if value is None:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"signal {self.key!r} not available")
        if self.numeric_range is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return _verdict(after, Disposition.UNKNOWN, self.name,
                                detail=f"signal {self.key!r} is not numeric: {value!r}")
            low, high = self.numeric_range
            if low <= numeric <= high:
                return _verdict(after, Disposition.PASS, self.name, actual=str(numeric))
            return _verdict(after, Disposition.FAIL, self.name,
                            expected=f"{low}..{high}", actual=str(numeric))
        if str(value) == self.expected:
            return _verdict(after, Disposition.PASS, self.name, actual=str(value))
        return _verdict(after, Disposition.FAIL, self.name,
                        expected=self.expected, actual=str(value))


@dataclass(slots=True)
class NoInputFor(Condition):
    """The game did nothing for a while - a hang detector."""

    name: str = "no_input_for"
    ms: int = 3000

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        if before is None:
            return _verdict(after, Disposition.UNKNOWN, self.name, detail="no baseline")
        elapsed = after.mono_ms - before.mono_ms
        score = vision.diff(
            before.frame.array, after.frame.array
        ).score
        if elapsed < self.ms:
            return _verdict(after, Disposition.UNKNOWN, self.name,
                            detail=f"only {elapsed:.0f}ms elapsed, need {self.ms}ms")
        if score <= 0.002:
            return _verdict(after, Disposition.FAIL, self.name,
                            detail=f"no visual change for {elapsed:.0f}ms")
        return _verdict(after, Disposition.PASS, self.name, detail=f"activity detected")


@dataclass(slots=True)
class AlwaysTrue(Condition):
    """Trivially satisfied. Used for steps whose only purpose is to capture evidence."""

    name: str = "always"

    def evaluate(self, before: Observation | None, after: Observation, vision: VisionPort) -> Verdict:
        return _verdict(after, Disposition.PASS, self.name)


# ----------------------------------------------------------------------- verifier


class Verifier:
    """Evaluates conditions. Deterministic, no AI, no network, no clock dependence."""

    def __init__(self, vision: VisionPort, *, clock: ClockPort | None = None) -> None:
        self._vision = vision
        self._clock = clock or SystemClock()
        self.evaluations = 0

    def evaluate(
        self,
        condition: Condition,
        before: Observation | None,
        after: Observation | None,
    ) -> Verdict:
        self.evaluations += 1
        name = getattr(condition, "name", "unknown")
        if after is None:
            # No observation means nothing can be concluded. UNKNOWN, never PASS.
            return _verdict(None, Disposition.UNKNOWN, name,
                            detail="no observation after execution")
        try:
            return condition.evaluate(before, after, self._vision)
        except Exception as exc:
            # A broken condition is an UNKNOWN, never a PASS. Fail-closed.
            return _verdict(after, Disposition.UNKNOWN, name,
                            detail=f"evaluation error: {type(exc).__name__}: {exc}")

    def evaluate_all(
        self,
        conditions: list[Condition],
        before: Observation | None,
        after: Observation | None,
    ) -> list[Verdict]:
        return [self.evaluate(c, before, after) for c in conditions]

    def wait_for(
        self,
        condition: Condition,
        before: Observation | None,
        fetch,
        *,
        timeout_ms: int = 10_000,
        poll_ms: int = 250,
        clock: ClockPort | None = None,
    ) -> Verdict:
        """Poll until a condition passes or the timeout expires.

        This is the honest replacement for ``sleep(N); assume success``. On timeout the
        result is whatever the last evaluation returned - commonly FAIL or UNKNOWN - never
        PASS.
        """
        clock = clock or self._clock
        start = clock.monotonic_ms()
        name = getattr(condition, "name", "unknown")
        verdict: Verdict | None = None
        while True:
            observation = fetch()
            if observation is None:
                clock.sleep_ms(poll_ms)
                if clock.monotonic_ms() - start > timeout_ms:
                    return Verdict(
                        disposition=Disposition.UNKNOWN,
                        condition=name,
                        detail=f"timed out after {timeout_ms}ms with no observation",
                    )
                continue
            verdict = self.evaluate(condition, before, observation)
            if verdict.ok:
                return verdict
            if clock.monotonic_ms() - start > timeout_ms:
                return Verdict(
                    disposition=verdict.disposition,
                    condition=verdict.condition,
                    detail=f"{verdict.detail} (timed out after {timeout_ms}ms)",
                    confidence=verdict.confidence,
                    frame_hash=verdict.frame_hash,
                    frame_index=verdict.frame_index,
                    mono_ms=verdict.mono_ms,
                    expected=verdict.expected,
                    actual=verdict.actual,
                )
            clock.sleep_ms(poll_ms)


__all__ = [
    "AlwaysTrue",
    "AnchorAbsent",
    "AnchorVisible",
    "ColorProbeMatches",
    "Condition",
    "NoChange",
    "NoInputFor",
    "RegionMatches",
    "ScreenChanged",
    "SignalEquals",
    "TextAbsent",
    "TextMatches",
    "Verdict",
    "Verifier",
    "WindowProperty",
]
