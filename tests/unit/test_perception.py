"""Perception: geometry, anchors, health, verification."""

from __future__ import annotations

import numpy as np
import pytest

from frameforge.kernel.events import Disposition
from frameforge.perception.assembler import AnchorSpec, FrameAssembler
from frameforge.perception.health import (
    DisplayHealthMonitor,
    HealthPolicy,
    HealthVerdict,
)
from frameforge.perception.verify import (
    AlwaysTrue,
    AnchorAbsent,
    AnchorVisible,
    NoChange,
    RegionMatches,
    ScreenChanged,
    SignalEquals,
    TextMatches,
    WindowProperty,
)
from frameforge.ports import fakes
from frameforge.ports.geometry import Point, Rect, RegionSpec, Size


class TestGeometry:
    def test_norm_rect_clamped_to_surface(self):
        surface = Size(1920, 1080)
        r = Rect.from_norm(0.9, 0.9, 0.5, 0.5, surface)
        assert r.right <= 1920 and r.bottom <= 1080

    def test_full_width_quadrant(self):
        r = Rect.from_norm(0.5, 0.0, 0.5, 1.0, Size(1920, 1080))
        assert r.as_tuple() == (960, 0, 960, 1080)

    def test_clamp_shrinks_rather_than_moves(self):
        """'The left quarter' must stay the left quarter on a narrower surface."""
        r = Rect(0, 0, 1920, 200)
        clamped = r.clamp_to(Size(1280, 720))
        assert clamped.x == 0 and clamped.width == 1280

    def test_containment(self):
        r = Rect(10, 10, 100, 50)
        assert r.contains(Point(10, 10))
        assert r.contains(Point(109, 59))
        assert not r.contains(Point(110, 60))

    def test_intersection(self):
        a, b = Rect(0, 0, 100, 100), Rect(50, 50, 100, 100)
        inter = a.intersection(b)
        assert inter is not None and inter.as_tuple() == (50, 50, 50, 50)
        assert a.intersection(Rect(200, 200, 10, 10)) is None

    def test_rescale_refuses_mismatched_aspect(self):
        with pytest.raises(ValueError, match="aspect"):
            Rect(0, 0, 100, 100).rescale_to(Size(1920, 1080), Size(100, 100))

    def test_rescale_preserves_same_aspect(self):
        out = Rect(0, 0, 960, 540).rescale_to(Size(1280, 720), Size(1920, 1080))
        assert out.as_tuple() == (0, 0, 640, 360)

    def test_region_spec_units(self):
        surface = Size(1920, 1080)
        assert RegionSpec(0, 0, 0.25, 1.0, "norm").to_rect(surface).as_tuple() == (0, 0, 480, 1080)
        assert RegionSpec(10, 20, 100, 50, "px").to_rect(surface).as_tuple() == (10, 20, 100, 50)

    def test_region_spec_rejects_bad_unit(self):
        with pytest.raises(ValueError):
            RegionSpec(0, 0, 1, 1, "furlongs")

    def test_norm_region_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            RegionSpec(0, 0, 1.5, 1.0, "norm")


class TestAnchors:
    def _observation(self, vision, ocr=None, anchors=(), window=None, health=None):
        assembler = FrameAssembler(vision)
        frame = fakes.make_frame(rgb=(10, 10, 10))
        if ocr is None:
            ocr = fakes.FakeOcr().read(frame.array)
        return assembler.assemble(
            frame, window=window, ocr=ocr,
            anchors=anchors, health=health,
        )

    def test_text_anchor_matches(self):
        base = fakes.make_frame(rgb=(10, 10, 10))
        ocr = fakes.FakeOcr(default=["START GAME"]).read(base.array)
        obs = self._observation(fakes.FakeVision(), ocr=ocr)
        specs = [AnchorSpec(name="start", kind="text", pattern=r"^START GAME$", threshold=0.5)]
        results = FrameAssembler(fakes.FakeVision()).evaluate_anchors(
            obs.frame, specs, obs.ocr
        )
        assert results[0].present and results[0].name == "start"

    def test_text_anchor_absent_is_reported_with_zero(self):
        obs = self._observation(fakes.FakeVision(), ocr=fakes.FakeOcr(default=["MENU"]).read(
            fakes.solid_image(640, 480, (10, 10, 10))))
        specs = [AnchorSpec(name="missing", kind="text", pattern="NEVER", threshold=0.5)]
        results = FrameAssembler(fakes.FakeVision()).evaluate_anchors(obs.frame, specs, obs.ocr)
        assert not results[0].present and results[0].score == 0.0

    def test_anchor_without_template_is_absent_not_crash(self):
        obs = self._observation(fakes.FakeVision())
        specs = [AnchorSpec(name="t", kind="template", template=None)]
        results = FrameAssembler(fakes.FakeVision()).evaluate_anchors(obs.frame, specs, obs.ocr)
        assert not results[0].present

    def test_confidence_drops_when_ocr_unavailable(self):
        from frameforge.perception.health import HealthReport

        assembler = FrameAssembler(fakes.FakeVision())
        frame = fakes.make_frame()
        healthy = assembler.assemble(frame, window=None, ocr=fakes.FakeOcr().read(frame.array))
        from frameforge.ports.ocr import OcrResult

        broken = assembler.assemble(
            frame, window=None, ocr=OcrResult(error="no backend"),
        )
        assert broken.confidence < healthy.confidence

    def test_fatal_health_zeroes_confidence(self):
        from frameforge.perception.health import HealthReport

        assembler = FrameAssembler(fakes.FakeVision())
        frame = fakes.make_frame()
        obs = assembler.assemble(
            frame, window=None, ocr=fakes.FakeOcr().read(frame.array),
            health=HealthReport(verdict=HealthVerdict.FATAL, capture="black"),
        )
        assert obs.confidence == 0.0


class TestHealth:
    def test_black_frame_is_fatal(self, vision):
        monitor = DisplayHealthMonitor(fakes.FakeWindow(), vision)
        frame = fakes.FakeCapture().grab()
        black = fakes.make_frame(
            array=np.zeros((100, 100, 3), np.uint8), width=100, height=100, content_hash="x",
        )
        report = monitor.observe(black)
        assert report.verdict is HealthVerdict.FATAL
        assert report.capture.value == "black"

    def test_missing_frame_pauses(self, vision):
        monitor = DisplayHealthMonitor(fakes.FakeWindow(), vision)
        assert monitor.observe(None).verdict is HealthVerdict.PAUSE

    def test_identical_frames_flag_watch_not_fatal(self, vision):
        """A static menu is legitimate; only genuine loss should be fatal."""
        monitor = DisplayHealthMonitor(
            fakes.FakeWindow(), vision, policy=HealthPolicy(frozen_after_frames=3)
        )
        capture = fakes.FakeCapture()
        capture.open(capture.surface)
        verdicts = [monitor.observe(capture.grab()).verdict for _ in range(6)]
        assert HealthVerdict.FATAL not in verdicts

    def test_topology_change_increments_epoch(self, vision):
        window = fakes.FakeWindow()
        monitor = DisplayHealthMonitor(window, vision)
        _topo, changed = monitor.refresh_topology()
        assert not changed and monitor.epoch == 0

    def test_varying_frames_are_ok(self, vision):
        monitor = DisplayHealthMonitor(fakes.FakeWindow(), vision)
        capture = fakes.FakeCapture()
        capture.open(capture.surface)
        verdicts = [monitor.observe(capture.grab()).verdict for _ in range(4)]
        assert all(v is HealthVerdict.OK for v in verdicts)


class TestVerification:
    def _obs(self, vision, image, ocr_text=()):
        assembler = FrameAssembler(vision)
        frame = fakes.make_frame(array=image, width=200, height=200)
        ocr = fakes.FakeOcr(default=list(ocr_text)).read(image)
        return assembler.assemble(frame, window=None, ocr=ocr)

    def test_screen_changed_passes_on_difference(self, vision):
        before = self._obs(vision, fakes.solid_image(200, 200, (0, 0, 0)))
        after = self._obs(vision, fakes.solid_image(200, 200, (255, 255, 255)))
        verdict = ScreenChanged().evaluate(before, after, vision)
        assert verdict.disposition is Disposition.PASS

    def test_screen_changed_fails_on_identity(self, vision):
        a = self._obs(vision, fakes.solid_image(200, 200, (10, 10, 10)))
        b = self._obs(vision, fakes.solid_image(200, 200, (10, 10, 10)))
        assert ScreenChanged().evaluate(a, b, vision).disposition is Disposition.FAIL

    def test_screen_changed_without_baseline_is_unknown(self, vision):
        after = self._obs(vision, fakes.solid_image(200, 200, (10, 10, 10)))
        verdict = ScreenChanged().evaluate(None, after, vision)
        assert verdict.disposition is Disposition.UNKNOWN

    def test_no_change_is_the_negative_assertion(self, vision):
        a = self._obs(vision, fakes.solid_image(200, 200, (10, 10, 10)))
        b = self._obs(vision, fakes.solid_image(200, 200, (10, 10, 10)))
        assert NoChange().evaluate(a, b, vision).disposition is Disposition.PASS

    def test_anchor_visible_pass_and_fail(self, vision):
        from frameforge.perception.assembler import AnchorResult

        base = self._obs(vision, fakes.solid_image(200, 200))
        present = type(base)(
            frame=base.frame, window=base.window, ocr=base.ocr,
            anchors=(AnchorResult("m", True, 0.95),), color_probes=(), health=base.health,
            confidence=1.0, mono_ms=0.0,
        )
        absent = type(base)(
            frame=base.frame, window=base.window, ocr=base.ocr,
            anchors=(AnchorResult("m", False, 0.1),), color_probes=(), health=base.health,
            confidence=1.0, mono_ms=0.0,
        )
        assert AnchorVisible(anchor="m").evaluate(None, present, vision).ok
        assert AnchorVisible(anchor="m").evaluate(None, absent, vision).failed
        assert AnchorAbsent(anchor="m").evaluate(None, absent, vision).ok

    def test_never_evaluated_anchor_is_unknown(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200))
        verdict = AnchorVisible(anchor="ghost").evaluate(None, obs, vision)
        assert verdict.disposition is Disposition.UNKNOWN

    def test_text_matches(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200), ["LOADING"])
        assert TextMatches(pattern="LOAD").evaluate(None, obs, vision).ok
        assert TextMatches(pattern="ABSENT").evaluate(None, obs, vision).failed

    def test_text_assertion_without_ocr_is_unknown(self, vision):
        from frameforge.ports.ocr import OcrResult

        assembler = FrameAssembler(vision)
        frame = fakes.make_frame()
        obs = assembler.assemble(frame, window=None, ocr=OcrResult(error="unavailable"))
        verdict = TextMatches(pattern="anything").evaluate(None, obs, vision)
        assert verdict.disposition is Disposition.UNKNOWN

    def test_window_property_without_identity_is_unknown(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200))
        verdict = WindowProperty(property="title", expected="x").evaluate(None, obs, vision)
        assert verdict.disposition is Disposition.UNKNOWN

    def test_signal_without_reader_is_unknown(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200))
        verdict = SignalEquals(key="hp", expected="50").evaluate(None, obs, vision)
        assert verdict.disposition is Disposition.UNKNOWN

    def test_signal_numeric_range(self, vision):
        class Reader:
            def read(self, key):
                return "37"

        obs = self._obs(vision, fakes.solid_image(200, 200))
        ok = SignalEquals(key="hp", numeric_range=(0, 100), reader=Reader()).evaluate(None, obs, vision)
        bad = SignalEquals(key="hp", numeric_range=(50, 100), reader=Reader()).evaluate(None, obs, vision)
        assert ok.ok and bad.failed

    def test_always_true(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200))
        assert AlwaysTrue().evaluate(None, obs, vision).ok

    def test_verdict_binds_evidence(self, vision):
        obs = self._obs(vision, fakes.solid_image(200, 200))
        verdict = AlwaysTrue().evaluate(None, obs, vision)
        assert verdict.frame_hash == "test-frame" and verdict.frame_index == 1

    def test_broken_condition_yields_unknown_not_pass(self, vision):
        from frameforge.perception.verify import Verifier

        class Exploding:
            name = "exploding"

            def evaluate(self, before, after, v):
                raise RuntimeError("boom")

        obs = self._obs(vision, fakes.solid_image(200, 200))
        verdict = Verifier(vision).evaluate(Exploding(), None, obs)
        assert verdict.disposition is Disposition.UNKNOWN
        assert "boom" in verdict.detail
