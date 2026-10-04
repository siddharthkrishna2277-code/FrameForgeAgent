"""In-memory fakes for every port.

These exist so that 100% of the engine can be tested with no display, no Windows APIs
and no game. They are not test-only toys: ``frameforge simulate`` runs the *real*
director against these, which is how CI and the QA harness exercise the full loop.

The fakes are deliberately strict. ``FakeInputPort`` records everything and can be
asserted on. ``FakeCapturePort`` can be scripted to produce a black frame, a frozen
frame, or a frame sequence that walks a scripted state machine - which is how the
health monitor, the change detector and the recovery ladder get tested deterministically.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from frameforge.ports.capture import CaptureHealth, CaptureStatus, Frame, Surface, SurfaceKind
from frameforge.ports.geometry import Point, Rect, Size
from frameforge.ports.input import InputCapabilities, Key, MouseButton, Primitive, PrimitiveType
from frameforge.ports.ocr import OcrCapabilities, OcrLine, OcrResult
from frameforge.ports.vision import (
    ChangeScore,
    ColorProbe,
    ColorProbeResult,
    TemplateMatch,
    VisionPort,
)
from frameforge.ports.window import (
    DisplayTopology,
    MonitorInfo,
    SessionState,
    TargetSpec,
    WindowInfo,
)


def solid_image(width: int, height: int, rgb: tuple[int, int, int] = (20, 20, 20)) -> np.ndarray:
    """A flat-colour image. Cheap to compare, and its hash is stable."""
    return np.full((height, width, 3), rgb, dtype=np.uint8)


def noisy_image(
    width: int,
    height: int,
    seed: int = 0,
    base: int = 40,
    spread: int = 60,
) -> np.ndarray:
    """Deterministic pseudo-random image, for change-detection tests."""
    rng = np.random.default_rng(seed)
    return rng.integers(
        base, base + spread, size=(height, width, 3), dtype=np.uint8
    )


def make_frame(
    array=None,
    *,
    width: int = 640,
    height: int = 480,
    rgb: tuple[int, int, int] = (30, 30, 30),
    index: int = 1,
    content_hash: str = "test-frame",
    mono_ms: float = 0.0,
):
    """Build a Frame directly, without needing an open capture source.

    Tests construct frames constantly; making them reach into FakeCapture just to get a
    holder object was producing ``type(None)(...)`` mistakes in test helpers.
    """
    from frameforge.ports.capture import Frame, Surface, SurfaceKind

    surface = Surface(kind=SurfaceKind.WINDOW, size=Size(width, height), hwnd=1000, label="test")
    if array is None:
        array = solid_image(width, height, rgb)
    return Frame(
        array=array, surface=surface, mono_ms=mono_ms, wall_ms=0.0, index=index,
        content_hash=content_hash,
    )


# ------------------------------------------------------------------------ capture


class FakeCapture:
    """Scripted capture source.

    ``script`` is a list of frames to hand out in order; when exhausted it repeats the
    last one. ``black_after`` / ``freeze_after`` let a test simulate the two classic
    capture failure modes.
    """

    name = "fake_capture"

    def __init__(
        self,
        surface: Surface | None = None,
        *,
        frame_rgb: tuple[int, int, int] = (30, 30, 30),
        script: list[np.ndarray] | None = None,
        black_after: int | None = None,
        freeze_after: int | None = None,
    ) -> None:
        self.surface = surface or Surface(
            kind=SurfaceKind.MONITOR, size=Size(1920, 1080), label="fake0"
        )
        self._rgb = frame_rgb
        self._script = list(script) if script else None
        self._cycle = itertools.count()
        self._black_after = black_after
        self._freeze_after = freeze_after
        self._open = False
        self.grab_count = 0
        self._last_array: np.ndarray | None = None
        self.closed = False

    def open(self, surface: Surface) -> None:
        self.surface = surface
        self._open = True

    def grab(self) -> Frame | None:
        if not self._open:
            return None
        n = next(self._cycle)
        self.grab_count = n
        if self._script:
            arr = self._script[min(n, len(self._script) - 1)]
        else:
            # Slowly varying content so change detection sees real differences.
            arr = solid_image(
                self.surface.size.width, self.surface.size.height,
                (self._rgb[0] + (n % 7) * 10, self._rgb[1], self._rgb[2]),
            )
        if self._black_after is not None and n >= self._black_after:
            arr = np.zeros_like(arr)
        if self._freeze_after is not None and n >= self._freeze_after and self._last_array is not None:
            arr = self._last_array
        self._last_array = arr
        return Frame(
            array=arr,
            surface=self.surface,
            mono_ms=float(n * 50),
            wall_ms=1_700_000_000_000.0 + n * 50,
            index=n,
            content_hash=f"fake{n}",
            capture_ms=2.0,
            source=self.name,
        )

    def status(self) -> CaptureStatus:
        if self._black_after is not None and self.grab_count >= self._black_after:
            return CaptureStatus(health=CaptureHealth.BLACK, detail="scripted black frame")
        if self._freeze_after is not None and self.grab_count >= self._freeze_after:
            return CaptureStatus(health=CaptureHealth.FROZEN, detail="scripted frozen frame")
        return CaptureStatus(health=CaptureHealth.OK, fps_estimate=20.0)

    def close(self) -> None:
        self.closed = True
        self._open = False


# --------------------------------------------------------------------------- input


@dataclass
class FakeInput:
    """Records every primitive instead of sending it.

    Also enforces the enabled gate exactly as the real adapter does, so the "no input
    while disarmed" invariant (stability criterion 4) is exercised in tests rather than
    merely asserted in prose.
    """

    name: str = "fake"
    primitives: list[Primitive] = field(default_factory=list)
    #: A genuine InputSafetyManager, so the fake port satisfies exactly the same contract
    #: as a live one. Anything the policy, controller or cleanup layers rely on is
    #: therefore exercised headlessly rather than only against real hardware.
    safety: object = None
    _enabled: bool = False
    position: tuple[int, int] = (0, 0)
    release_all_calls: int = 0

    def __post_init__(self) -> None:
        if self.safety is None:
            from frameforge.actions.safety import InputSafetyManager

            self.safety = InputSafetyManager()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool, *, thorough: bool = True) -> None:
        """Accept the same signature as the live port.

        A fake that accepts fewer keywords than the real adapter is a fake that can pass
        while the real one fails - which is precisely how the click-path bug survived 300
        tests. Contract parity matters more than convenience here.
        """
        self._enabled = value
        # Arm/disarm the safety manager in lockstep, as the live adapter does. The live
        # SendInputPort.set_enabled() says so explicitly - "the two must move together or
        # input silently stops" - and the fake did not, which is why it could not observe a
        # disarm and why the live-only defect went unnoticed.
        if self.safety is not None:
            if value:
                self.safety.arm()
            else:
                self.safety.release_all(thorough=thorough)
                self.safety.disarm()

    def send(self, primitive: Primitive) -> None:
        # Both gates are consulted: the fake's own flag, and the safety manager's. They can
        # disagree, and when they do the real adapter refuses the press while the fake would
        # have accepted it - which is exactly how a live-only defect stayed invisible to the
        # whole suite. The first live POC run found one: release_all() used to disarm the
        # manager, so later presses were refused in reality and accepted here.
        from frameforge.adapters.input.sendinput import is_release

        if not self._enabled:
            return
        if self.safety is not None and not self.safety.enabled and not is_release(primitive):
            return
        self.primitives.append(primitive)
        if primitive.kind == PrimitiveType.MOUSE_MOVE_ABS:
            self.position = (primitive.x, primitive.y)
        elif primitive.kind == PrimitiveType.MOUSE_MOVE_REL:
            self.position = (self.position[0] + primitive.dx, self.position[1] + primitive.dy)

    def send_batch(self, primitives: list[Primitive]) -> int:
        if not self._enabled:
            return 0
        for p in primitives:
            self.send(p)
        return len(primitives)

    def release_all(self, *, thorough: bool = True) -> dict:
        self.release_all_calls += 1
        if self.safety is not None:
            return self.safety.release_all(thorough=thorough)
        return {"keys_released": [], "buttons_released": [], "errors": []}

    def capabilities(self) -> InputCapabilities:
        return InputCapabilities(
            absolute_mouse=True, relative_mouse=True, mouse_buttons=3,
            scroll=True, unicode=True, notes=("fake",),
        )

    # ------------------------------------------------------------------ assertions

    def kinds(self) -> list[str]:
        return [str(p.kind) for p in self.primitives]

    def clicks(self) -> list[Primitive]:
        return [p for p in self.primitives if p.kind == PrimitiveType.MOUSE_BUTTON and p.down]

    def keys_down(self) -> list[Key]:
        return [p.key for p in self.primitives if p.kind == PrimitiveType.KEY and p.down and p.key]

    def keys_up(self) -> list[Key]:
        return [p.key for p in self.primitives if p.kind == PrimitiveType.KEY and not p.down and p.key]

    def last_click_at(self) -> Point | None:
        for p in reversed(self.primitives):
            if p.kind == PrimitiveType.MOUSE_MOVE_ABS:
                return Point(p.x, p.y)
        return None


# -------------------------------------------------------------------------- vision


class FakeVision:
    """OpenCV-free vision. Uses numpy reductions so behaviour is predictable.

    ``template_scores`` lets a test say "template A scores 0.95, B scores 0.1" without
    drawing anything, which is what makes Tier-0 planner tests one-liners.
    """

    name = "fake_vision"

    def __init__(self, template_scores: dict[str, float] | None = None) -> None:
        self.template_scores = dict(template_scores or {})
        self.encode_calls = 0

    def to_gray(self, image: np.ndarray) -> np.ndarray:
        return image.astype(np.float32).mean(axis=2)

    def encode_png(self, image: np.ndarray) -> bytes:
        self.encode_calls += 1
        return b"\x89PNG\r\n\x1a\n" + image.tobytes()[:64]

    def match_template(
        self,
        haystack: np.ndarray,
        template: np.ndarray,
        threshold: float = 0.8,
        method: str = "ccoeff_normed",
    ) -> TemplateMatch:
        h, w = template.shape[:2]
        return TemplateMatch(
            score=1.0 if threshold <= 0.0 else 0.5,
            rect=Rect(0, 0, w, h),
            template_size=(w, h),
            method=method,
        )

    def match_templates(
        self, haystack: np.ndarray, templates: list[np.ndarray], threshold: float = 0.8
    ) -> list[TemplateMatch]:
        return [self.match_template(haystack, t, threshold) for t in templates]

    def diff(self, a: np.ndarray, b: np.ndarray) -> ChangeScore:
        if a.shape != b.shape:
            return ChangeScore(score=1.0, method="shape_mismatch")
        d = np.abs(a.astype(np.int16) - b.astype(np.int16))
        return ChangeScore(
            score=float((d > 8).mean()),
            method="absdiff_fraction",
            detail={"mean_abs": float(d.mean())},
        )

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return 1.0 - self.diff(a, b).score

    def probe_color(
        self, image: np.ndarray, probe: ColorProbe, rect: Rect | None = None
    ) -> ColorProbeResult:
        return ColorProbeResult(ratio=1.0, matched=True)

    def region_activity(self, image: np.ndarray, rect: Rect | None = None) -> float:
        if image.size == 0:
            return 0.0
        g = self.to_gray(image)
        return float(np.abs(np.diff(g)).mean() / 255.0)

    def is_black(self, image: np.ndarray, threshold: float = 3.0) -> bool:
        return float(image.astype(np.float32).mean()) < threshold

    def downscale(self, image: np.ndarray, max_dimension: int) -> np.ndarray:
        h, w = image.shape[:2]
        scale = max_dimension / max(h, w)
        if scale >= 1.0:
            return image
        nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
        idx = (np.arange(nh) * (h / nh)).astype(int)
        idy = (np.arange(nw) * (w / nw)).astype(int)
        return image[idx][:, idy]


# ------------------------------------------------------------------------------ ocr


class FakeOcr:
    """OCR stub driven by a scripted per-index line set.

    ``script`` maps a frame index to the text lines that "are on screen". Tests that
    need a menu at step 1 and gameplay at step 5 set ``script`` accordingly.
    """

    name = "fake_ocr"

    def __init__(self, script: dict[int, list[str]] | None = None, default: list[str] | None = None) -> None:
        self.script = dict(script or {})
        self.default = list(default or [])
        self.calls = 0

    def read(self, image: np.ndarray) -> OcrResult:
        self.calls += 1
        h, w = image.shape[:2]
        texts = self.script.get(self.calls - 1, self.default)
        lines = tuple(
            OcrLine(text=t, rect=Rect(10 + i * 10, 10 + i * 20, 200, 18), confidence=0.99)
            for i, t in enumerate(texts)
        )
        return OcrResult(lines=lines, engine=self.name, mono_ms=1.0, width=w, height=h)

    def capabilities(self) -> OcrCapabilities:
        return OcrCapabilities(available=True, engines=(self.name,), primary=self.name)


class NullOcr:
    """No OCR available. Reports honestly instead of pretending."""

    name = "null_ocr"

    def read(self, image: np.ndarray) -> OcrResult:
        return OcrResult(engine=self.name, error="no OCR backend available")

    def capabilities(self) -> OcrCapabilities:
        return OcrCapabilities(
            available=False, notes=("vision-only mode: text assertions will be UNKNOWN",)
        )


# -------------------------------------------------------------------------- window


class FakeWindow:
    """Window/session/input stub with a settable foreground window.

    Focus-drift tests simply set ``foreground_hwnd`` to something else and assert that
    the executor blocked input - which is exactly the drill stability criterion 3
    requires.
    """

    name = "fake_window"

    def __init__(
        self,
        spec: TargetSpec | None = None,
        *,
        hwnd: int = 1000,
        title: str = "Test Window",
        class_name: str = "TestWindowClass",
        pid: int = 4242,
        process_name: str = "testgame.exe",
        size: Size | None = None,
        session: SessionState = SessionState.ACTIVE,
    ) -> None:
        size = size or Size(1280, 720)
        self.spec = spec or TargetSpec(title_regex="Test", process_name=process_name)
        self.info = WindowInfo(
            hwnd=hwnd,
            title=title,
            class_name=class_name,
            pid=pid,
            process_name=process_name,
            client_rect=Rect(0, 0, size.width, size.height),
            window_rect=Rect(0, 0, size.width, size.height),
            is_foreground=True,
            client_origin_screen=Point(0, 0),
        )
        self.foreground_hwnd = hwnd
        self.session = session
        self._idle_ms = 0
        self.windows = [self.info]
        #: Set to a window title to simulate the target being covered by a popup.
        self.occluded_by: str | None = None
        self.occluded_fraction: float = 0.0

    def enumerate_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def find_window(self, spec: TargetSpec) -> WindowInfo | None:
        for w in self.windows:
            if spec.title_regex:
                import re

                if not re.search(spec.title_regex, w.title, re.IGNORECASE):
                    continue
            if spec.class_name and w.class_name != spec.class_name:
                continue
            if spec.process_name and w.process_name.lower() != spec.process_name.lower():
                continue
            if spec.pid and w.pid != spec.pid:
                continue
            if spec.exe_names and w.process_name.lower() not in {
                e.lower() for e in spec.exe_names
            }:
                continue
            return w
        return None

    def window_info(self, hwnd: int) -> WindowInfo | None:
        return next((w for w in self.windows if w.hwnd == hwnd), None)

    def foreground(self) -> WindowInfo | None:
        return next((w for w in self.windows if w.hwnd == self.foreground_hwnd), None)

    def set_foreground(self, hwnd: int) -> bool:
        self.foreground_hwnd = hwnd
        return True

    def monitors(self) -> DisplayTopology:
        return DisplayTopology(
            monitors=(
                MonitorInfo(0, "\\\\.\\DISPLAY1", Rect(0, 0, 1920, 1080), True, 1.0),
            ),
            virtual_rect=Rect(0, 0, 1920, 1080),
            primary_index=0,
        )

    def last_input_info(self) -> int:
        return self._idle_ms

    def idle_ms(self) -> int:
        return self._idle_ms

    def set_idle_ms(self, ms: int) -> None:
        self._idle_ms = ms

    def occlusion(self, hwnd, client_origin, size):
        """Scriptable occlusion, so the health path is testable headlessly."""
        from frameforge.ports.window import OcclusionInfo

        if self.occluded_by is None:
            return OcclusionInfo(occluded=False, occluded_fraction=0.0)
        return OcclusionInfo(
            occluded=True,
            occluded_fraction=self.occluded_fraction,
            covering_titles=(self.occluded_by,),
            covering_classes=("Popup",),
        )

    def session_state(self) -> SessionState:
        return self.session

    def console_session_id(self) -> int:
        return 1 if self.session.interactive else 0xFFFFFFFF


__all__ = [
    "FakeCapture",
    "make_frame",
    "FakeInput",
    "FakeOcr",
    "FakeVision",
    "FakeWindow",
    "NullOcr",
    "noisy_image",
    "solid_image",
]
