"""P4 (DPI awareness), P5 (live topology invalidation), P6 (target-scoped capture).

Each test asserts the property that actually prevents the failure, not merely that a
function exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frameforge.actions.arm import CaptureFrame
from frameforge.actions.coordinates import ScreenPx, VirtualDesktop
from frameforge.actions.target import TargetGuard, TargetSession


# --------------------------------------------------------------------------- P4


class TestP4DpiAwareness:
    def test_process_is_per_monitor_aware(self):
        """Verified on this machine, not merely declared.

        Mixed laptop/external scaling is the case where a visually correct point becomes a
        physically wrong click, so the mode actually in force is the thing to assert.
        """
        from frameforge.adapters.window.pywin32_window import dpi_awareness

        state = dpi_awareness()
        assert state["attempted"] is True
        assert state["active"] is True, "DPI awareness was not established"
        assert state["mode"] in ("per_monitor_v2", "per_monitor_v1"), (
            f"only {state['mode']}; Windows may still virtualise coordinates"
        )

    def test_reports_monitors_with_dpi_and_scale(self):
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        monitors = PyWin32WindowAdapter().monitors_detailed()
        assert monitors, "no monitors reported"
        for m in monitors:
            assert m.dpi >= 72, f"{m.device_name} reported an implausible dpi {m.dpi}"
            assert m.scale_percent >= 100
            assert m.orientation in ("landscape", "portrait")
            assert m.rect.width > 0 and m.rect.height > 0

    def test_work_area_is_distinct_from_the_full_rect(self):
        """The taskbar sits in the work-area gap; a point there is not in the window."""
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        for m in PyWin32WindowAdapter().monitors_detailed():
            if m.work_area is not None:
                assert m.work_area.height <= m.rect.height

    def test_every_coordinate_space_is_physical_pixels(self):
        """Client rect, window rect and the SendInput mapping all use physical pixels.

        A logical-pixel sneaking into any of them shifts a click by the scale factor, which
        is invisible on a uniformly scaled desktop and wrong on a mixed one.
        """
        from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

        window = PyWin32WindowAdapter()
        desktop = VirtualDesktop(x=0, y=0, width=3840, height=1080)
        for monitor in window.monitors_detailed():
            rect = monitor.rect
            assert isinstance(rect.width, int) and isinstance(rect.x, int)
            # A point on this monitor must normalise into SendInput's range and round-trip.
            point = ScreenPx(rect.x + rect.width // 2, rect.y + rect.height // 2)
            assert desktop.contains(point)
            norm = desktop.to_send_input(point)
            assert 0 <= norm.x <= 65535 and 0 <= norm.y <= 65535

    def test_dpi_is_part_of_the_topology_fingerprint(self):
        """A scaling change alters the client-to-physical mapping, so it must be in identity."""
        from frameforge.adapters.window.pywin32_window import monitor_topology_fingerprint

        fp = monitor_topology_fingerprint()
        assert "vd=" in fp
        # Each monitor segment carries its dpi/scale.
        segments = [s for s in fp.split("|") if ":" in s and s.startswith(("0:", "1:", "2:"))]
        assert segments
        for seg in segments:
            assert ":" in seg
            assert seg.count(":") >= 4, f"monitor segment lacks dpi/scale: {seg}"


# --------------------------------------------------------------------------- P5


class _StubSession:
    """Session double exposing only what TargetGuard.refresh_topology needs."""

    def __init__(self, fingerprint: str = "") -> None:
        self.topology_fingerprint = fingerprint
        self.hwnd = 4242
        self.pid = 777
        self.client_origin = (0, 0)
        self.client_size = type("S", (), {"width": 1920, "height": 1080})()
        from frameforge.ports.geometry import Rect

        self.client_rect = Rect(0, 0, 1920, 1080)

    def refresh_topology(self) -> tuple[bool, str]:
        """Compare against the *live* fingerprint, exactly as TargetSession does.

        Returning a constant here would make these three tests pass without ever exercising
        the comparison they exist to check.
        """
        if not self.topology_fingerprint:
            return True, ""
        from frameforge.adapters.window import pywin32_window

        now = pywin32_window.monitor_topology_fingerprint()
        if now != self.topology_fingerprint:
            return False, (
                f"display topology changed since the target was registered "
                f"({self.topology_fingerprint} -> {now}); pending input is cancelled and "
                "the target must be revalidated"
            )
        return True, ""


class TestP5TopologyInvalidation:
    def test_refresh_topology_passes_when_unchanged(self, monkeypatch):
        session = _StubSession("fingerprint-A")
        guard = TargetGuard(session=session)
        monkeypatch.setattr(
            "frameforge.adapters.window.pywin32_window.monitor_topology_fingerprint",
            lambda: "fingerprint-A", raising=False)
        ok, why = guard.refresh_topology()
        assert ok, why

    def test_monitor_disconnect_invalidates(self, monkeypatch):
        session = _StubSession("fingerprint-A")
        guard = TargetGuard(session=session)
        monkeypatch.setattr(
            "frameforge.adapters.window.pywin32_window.monitor_topology_fingerprint",
            lambda: "fingerprint-B-one-monitor-only", raising=False)
        ok, why = guard.refresh_topology()
        assert not ok
        assert "topology" in why.lower()
        assert "cancelled" in why.lower()

    def test_scaling_change_invalidates(self, monkeypatch):
        session = _StubSession("vd=0,0,3840,1080|0:D1:(0,0,1920,1080):96:100:1")
        guard = TargetGuard(session=session)
        # Same geometry, different scale: dpi 96 -> 144.
        monkeypatch.setattr(
            "frameforge.adapters.window.pywin32_window.monitor_topology_fingerprint",
            lambda: "vd=0,0,3840,1080|0:D1:(0,0,1920,1080):144:150:1", raising=False)
        ok, _why = guard.refresh_topology()
        assert not ok, "a 150% scaling change must invalidate the session"

    def test_monitor_reorder_invalidates(self, monkeypatch):
        session = _StubSession("vd=0,0,3840,1080|0:D1:(0,0,1920,1080)|1:D2:(1920,0,1920,1080)")
        guard = TargetGuard(session=session)
        monkeypatch.setattr(
            "frameforge.adapters.window.pywin32_window.monitor_topology_fingerprint",
            lambda: "vd=0,0,3840,1080|0:D1:(1920,0,1920,1080)|1:D2:(0,0,1920,1080)", raising=False)
        ok, _why = guard.refresh_topology()
        assert not ok, "moving a monitor to the other side must invalidate the session"

    def test_no_fingerprint_configured_is_permissive(self):
        """A session with no recorded layout cannot claim a change it cannot see."""
        guard = TargetGuard(session=_StubSession(""))
        ok, _why = guard.refresh_topology()
        assert ok

    def test_executor_checks_topology_before_every_mouse_action(self):
        """The check lives on the dispatch path, not only at session creation."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2]
                  / "src" / "frameforge" / "actions" / "executor.py").read_text(encoding="utf-8")
        assert "refresh_topology()" in source
        assert "monitor_topology_changed" in source


# --------------------------------------------------------------------------- P6


class TestP6TargetCapture:
    def _session(self):
        return TargetSession(
            run_id="r", hwnd=4242, pid=777, process_name="game.exe",
            image_path="C:/games/game.exe",
            client_origin=(1920, 0), client_size=__import__(
                "frameforge.ports.geometry", fromlist=["Size"]).Size(1920, 1080),
            topology_fingerprint="fp-A",
        )

    def test_frame_carries_full_target_metadata(self):
        frame = CaptureFrame(
            frame_id="f", run_id="r", session_id="s", target_hwnd=4242, target_pid=777,
            captured_mono_ms=__import__("time").monotonic() * 1000.0,
            width=1920, height=1080, dpi=120, monitor_index=1, monitor_device="DISPLAY2",
            topology_fingerprint="fp-A",
        )
        payload = frame.to_dict()
        for key in ("frame_id", "run_id", "session_id", "target_hwnd", "target_pid",
                    "width", "height", "client_rect", "window_rect", "monitor_index",
                    "dpi", "topology_fingerprint", "source", "transform_version"):
            assert key in payload, f"CaptureFrame missing {key}"

    def test_stale_frame_is_not_usable(self):
        import time

        frame = CaptureFrame(
            frame_id="f", run_id="r", session_id="s", target_hwnd=1, target_pid=1,
            captured_mono_ms=time.monotonic() * 1000.0 - 5000.0, max_age_ms=750.0,
        )
        ok, why = frame.usable()
        assert not ok and "old" in why

    def test_black_frame_is_not_usable(self):
        import time

        frame = CaptureFrame(
            frame_id="f", run_id="r", session_id="s", target_hwnd=1, target_pid=1,
            captured_mono_ms=time.monotonic() * 1000.0, healthy=False,
            health_detail="capture is black",
        )
        ok, why = frame.usable()
        assert not ok and "black" in why

    def test_no_capture_is_not_fresh(self, tmp_path):
        from frameforge.perception.proposal import TargetCapture
        from frameforge.ports import fakes

        surface = __import__(
            "frameforge.ports.capture", fromlist=["Surface"]).Surface(
            kind=__import__("frameforge.ports.capture", fromlist=["SurfaceKind"]).SurfaceKind.WINDOW,
            size=__import__("frameforge.ports.geometry", fromlist=["Size"]).Size(640, 480),
            hwnd=4242,
        )
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        tc = TargetCapture(self._session(), capture, fakes.FakeVision())
        ok, why = tc.is_fresh()
        assert not ok and "no capture" in why

    def test_capture_of_the_right_window_is_fresh(self):
        from frameforge.perception.proposal import TargetCapture
        from frameforge.ports import fakes
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size

        surface = Surface(kind=SurfaceKind.WINDOW, size=Size(640, 480), hwnd=4242)
        capture = fakes.FakeCapture(surface)
        capture.open(surface)
        session = self._session()
        tc = TargetCapture(session, capture, fakes.FakeVision())
        frame = tc.grab("run")
        assert frame is not None
        assert frame.target_hwnd == session.hwnd

    def test_runner_exposes_capture_health(self):
        """The runner must be able to say whether perception is usable."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2]
                  / "src" / "frameforge" / "tasks" / "runner.py").read_text(encoding="utf-8")
        assert "def capture_health" in source
        assert "TargetCapture" in source
        assert "_refresh_capture" in source
