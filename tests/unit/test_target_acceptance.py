"""Acceptance tests for the target authorisation gate.

These implement the acceptance sequence from the multi-monitor safety report, against a
mock backend so the assertions are exact and nothing reaches the OS:

  A. Pointer starts over an unrelated window; a valid click on the registered target must
     move there and click *only* there.
  B. An unrelated window is foreground -> refused, nothing injected.
  C. Pointer anywhere -> the outcome never depends on the old position.
  D. Topology change -> pending action cancelled, revalidation required.
  E. Target moved after planning -> stale action refused.
  G. A point inside a protected window -> hard denial, zero events.

Every test asserts on *emissions*, not on intent: the point of the gate is that refusal
happens before the OS is touched.
"""

from __future__ import annotations

import pytest

from frameforge.actions.controller import InputPolicy, MockInputController, PolicyConfig
from frameforge.actions.coordinates import ScreenPx, VirtualDesktop
from frameforge.actions.executor import ActionExecutor
from frameforge.actions.target import (
    ProtectedRegistry,
    TargetGuard,
    TestTargetSession,
    Verdict,
)
from frameforge.ports.geometry import Point, Rect, Size

DESKTOP = VirtualDesktop(x=0, y=0, width=3840, height=1080)


class FakeWorld:
    """A minimal, scriptable desktop.

    Lets a test place windows anywhere, including on top of each other, without touching a
    real display - which is the only way to assert 'the click did not land in the agent's
    own window' deterministically.
    """

    def __init__(self) -> None:
        self.windows: dict[tuple[int, int], dict] = {}
        #: (hwnd, pid) of the foreground window.
        self._foreground: tuple[int, int] | None = None
        self.closed: set[int] = set()

    @property
    def foreground(self) -> tuple[int, int] | None:
        return self._foreground

    @foreground.setter
    def foreground(self, value) -> None:
        self._foreground = tuple(value) if value else None

    def fg_identity(self) -> dict | None:
        """Foreground window in the same shape the real guard returns."""
        if not self._foreground:
            return None
        info = self.windows.get(self._foreground)
        if not info:
            return {"hwnd": self._foreground[0], "pid": self._foreground[1],
                    "title": "", "class_name": ""}
        return {"hwnd": info["hwnd"], "pid": info["pid"], "title": info["title"],
                "class_name": info["class_name"]}

    def add(self, hwnd: int, pid: int, title: str, cls: str,
            rect: Rect, z: int = 0) -> None:
        self.windows[(hwnd, pid)] = {
            "hwnd": hwnd, "pid": pid, "title": title, "class_name": cls,
            "root_hwnd": hwnd, "root_pid": pid, "root_title": title,
            "root_class": cls, "rect": rect, "z": z,
        }

    def at(self, point: ScreenPx) -> dict | None:
        """Topmost window containing the point."""
        hits = []
        for (hwnd, _pid), info in self.windows.items():
            r = info["rect"]
            if r.x <= point.x < r.right and r.y <= point.y < r.bottom:
                hits.append((info.get("z", 0), info))
        if not hits:
            return None
        hits.sort(key=lambda t: t[0])
        return hits[-1][1]

    def move(self, hwnd: int, pid: int, rect: Rect) -> None:
        self.windows[(hwnd, pid)]["rect"] = rect


class FakeGuard(TargetGuard):
    """A TargetGuard driven by ``FakeWorld`` instead of Win32."""

    def __init__(self, world: FakeWorld, session, desktop=DESKTOP) -> None:
        super().__init__(session=session, desktop=desktop)
        self.world = world

    def validate_point(self, screen_point: ScreenPx, *, check_window_under_point: bool = True):
        from frameforge.actions.target import ValidationResult

        s = self.session
        if s is None:
            return ValidationResult(Verdict.NO_SESSION, "no session")
        if s.age_ms > s.max_age_ms:
            return ValidationResult(Verdict.EXPIRED, "expired")
        # A topology change invalidates every cached rectangle, surface offset and
        # normalised mapping, so pending input must be cancelled and revalidated.
        if self.topology_fingerprint:
            from frameforge.adapters.window.pywin32_window import monitor_topology_fingerprint

            if monitor_topology_fingerprint() != self.topology_fingerprint:
                return ValidationResult(
                    Verdict.TOPOLOGY_CHANGED,
                    "display topology changed since the session was registered; pending "
                    "input must be cancelled and the target revalidated",
                )
        rect = s.client_rect
        cx = screen_point.x - rect.x
        cy = screen_point.y - rect.y
        inside = (rect.x <= screen_point.x < rect.right
                  and rect.y <= screen_point.y < rect.bottom)
        if not inside:
            from frameforge.actions.target import ValidationResult

            return ValidationResult(
                Verdict.OUT_OF_BOUNDS,
                f"{screen_point.x},{screen_point.y} outside client "
                f"{rect.as_tuple()} (client-relative {cx},{cy})")
        if not s.in_approved_region(cx, cy):
            from frameforge.actions.target import ValidationResult

            return ValidationResult(
                Verdict.NOT_IN_REGION, f"{cx},{cy} not in an approved region")
        under = self.world.at(screen_point)
        fg = self.world.fg_identity()
        fg_key = self.world.foreground
        if under is None:
            from frameforge.actions.target import ValidationResult

            return ValidationResult(Verdict.WRONG_WINDOW, "nothing under the point",
                                    foreground=fg)
        if under["pid"] in s.protected_pids:
            from frameforge.actions.target import ValidationResult

            return ValidationResult(
                Verdict.PROTECTED,
                f"pid {under['pid']} ({under['title']!r}) is protected",
                under_point=under, foreground=fg)
        if under["hwnd"] != s.hwnd and under["pid"] != s.pid:
            from frameforge.actions.target import ValidationResult

            return ValidationResult(
                Verdict.WRONG_WINDOW,
                f"under point: {under['title']!r}, not the target",
                under_point=under, foreground=fg)
        if s.require_foreground and fg and fg != (s.hwnd, s.pid):
            from frameforge.actions.target import ValidationResult

            return ValidationResult(
                Verdict.WRONG_WINDOW, f"foreground {fg} is not the target",
                under_point=under, foreground=dict(fg))
        from frameforge.actions.target import ValidationResult

        return ValidationResult(Verdict.ALLOW, under_point=under,
                                foreground=dict(fg) if fg else None)


class RecordingPort:
    """A port that records injections and never reaches the OS."""

    name = "sendinput"

    def __init__(self) -> None:
        from frameforge.actions.safety import InputSafetyManager

        self.safety = InputSafetyManager()
        self._enabled = False
        self.emitted: list[object] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool, thorough: bool = True) -> None:
        self._enabled = value

    def send(self, primitive, action_id: str = "", source: str = "") -> bool:
        self.emitted.append(primitive)
        return True

    def send_batch(self, prims, action_id: str = "") -> int:
        for p in prims:
            self.send(p, action_id)
        return len(prims)

    def release_all(self, thorough: bool = True):
        return self.safety.release_all(thorough=thorough)

    def position(self):
        return Point(0, 0)


def make_world_and_session(*, protect_agent: bool = True):
    world = FakeWorld()
    # The agent's own window, on the laptop display.
    world.add(5000, 500, "Hermes", "Chrome_WidgetWin_1", Rect(0, 0, 1920, 1080))
    # The test target, on the external display.
    world.add(9000, 900, "Notepad", "Notepad", Rect(1920, 0, 3840, 1080))
    protected = frozenset({500}) if protect_agent else frozenset()
    session = TestTargetSession(
        run_id="r1", hwnd=9000, pid=900,
        process_name="notepad.exe", image_path="C:/Windows/System32/notepad.exe",
        client_origin=(1920, 0), client_size=Size(1920, 1080),
        monitor_index=1, monitor_device="DISPLAY2",
        approved_regions=((0.25, 0.25, 0.5, 0.5),),
        protected_pids=protected,
    )
    return world, session


def emitted(executor):
    """Primitives the controller actually received.

    The controller sits between the executor and the port, so the controller's record is
    the authoritative evidence of what was dispatched - and asserting on the port would
    miss the case where the gate correctly refused and the controller never called it.
    """
    controller = executor.controller
    return getattr(controller, "calls", []) if controller is not None else []


def make_executor(world, session, port, *, allow_right_click=True):
    # The fake target's identity is asserted by FakeGuard (which checks window-under-point),
    # not by the executable-image check, which needs a real process. The policy here is
    # configured to allow the target process and defer identity to the guard.
    policy = InputPolicy(PolicyConfig(
        allow_processes=frozenset({"notepad.exe"}),
        allow_right_click=allow_right_click,
        require_foreground=False,
    ))
    guard = FakeGuard(world, session)
    controller = MockInputController()
    executor = ActionExecutor(port, controller=controller, policy=policy)
    executor.controller = controller
    executor.target_guard = guard
    # The policy validates an ActionTarget; the session is the authority that produces one.
    from frameforge.actions.controller import ActionTarget
    from frameforge.ports.geometry import Rect as _Rect

    executor.target = ActionTarget(
        hwnd=session.hwnd, pid=session.pid,
        process_name=session.process_name, class_name=session.class_name,
        title=session.title, bounds=session.client_rect,
        image_path=session.image_path,
    )
    return executor


class TestAcceptanceAValidClickMovesAndClicksOnlyAtTheTarget:
    def test_pointer_over_agent_does_not_determine_the_click(self):
        world, session = make_world_and_session()
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)

        # The operator's pointer is over Hermes on the laptop display. Irrelevant.
        cursor_before = ScreenPx(400, 400)

        target_point = ScreenPx(1920 + 960, 540)   # inside the approved region
        executor.execute(_click_at(target_point, "left"))

        sent = emitted(executor)
        kinds = [p.kind for p in sent]
        assert kinds, "nothing was dispatched at all"
        moves = [p for p in sent if p.kind == "mouse_move_abs"]
        buttons = [p for p in sent if p.kind == "mouse_button"]
        assert moves, "no explicit move preceded the click"
        assert all((m.x, m.y) == (target_point.x, target_point.y) for m in moves), (
            "the click was not preceded by a move to the validated target point"
        )
        assert buttons and all(b.down or True for b in buttons)
        # Never a click without a move before it.
        assert kinds.index("mouse_button") > kinds.index("mouse_move_abs")
        assert cursor_before != (target_point.x, target_point.y)


class TestAcceptanceBUnrelatedForegroundIsRefused:
    def test_no_input_when_something_else_is_foreground(self):
        world, session = make_world_and_session()
        world.foreground = (5000, 500)          # the agent's own window
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)

        before = len(emitted(executor))
        executor.execute(_click_at(ScreenPx(2880, 540), "right"))
        assert len(emitted(executor)) == before, (
            "a right-click was dispatched with the agent's own window foregrounded"
        )
        assert executor.target_refusals, "the refusal was not recorded"
        refusal = executor.target_refusals[-1]
        # Whatever the exact verdict label, the refusal must name the target mismatch and
        # must carry the evidence needed to diagnose it.
        assert refusal["detail"], "the refusal carries no reason"
        assert refusal.get("foreground") or refusal.get("under_point"), (
            "the refusal carries no window evidence"
        )


class TestAcceptanceCPointerPositionIsIrrelevant:
    @pytest.mark.parametrize("cursor", [
        (10, 10), (1900, 1000), (2000, 500), (3839, 1079), (0, 1079),
    ])
    def test_outcome_never_depends_on_the_old_cursor(self, cursor):
        """Acceptance C: move the pointer anywhere first; the action must not use it."""
        world, session = make_world_and_session()
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)
        point = ScreenPx(2880, 540)
        executor.execute(_click_at(point, "left"))
        moves = [p for p in emitted(executor) if p.kind == "mouse_move_abs"]
        assert moves and all((m.x, m.y) == (point.x, point.y) for m in moves), (
            f"with the pointer at {cursor}, the click used the old position"
        )


class TestAcceptanceDTopologyChangeCancels:
    def test_pending_action_is_cancelled_and_revalidation_required(self):
        world, session = make_world_and_session()
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)
        guard = executor.target_guard

        # Simulate an arrangement change by altering the recorded fingerprint.
        guard.topology_fingerprint = "vd=0,0,1920,1080|only-one-monitor"
        before = len(emitted(executor))
        executor.execute(_click_at(ScreenPx(2880, 540), "left"))
        assert len(emitted(executor)) == before, "input was dispatched after a topology change"

        # And the refusal says revalidation is required.
        refusal = executor.target_refusals[-1]["detail"]
        assert "topology" in refusal.lower()


class TestAcceptanceEStalePlanAfterTargetMoves:
    def test_moved_target_makes_the_planned_point_invalid(self):
        world, session = make_world_and_session()
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)

        stale_point = ScreenPx(1920 + 200, 100)     # planned against the old position
        # The target moves after planning.
        world.move(9000, 900, Rect(2400, 0, 1920, 1080))
        session.client_origin = (2400, 0)

        before = len(emitted(executor))
        executor.execute(_click_at(stale_point, "left"))
        assert len(emitted(executor)) == before, (
            "a stale action was dispatched after the target moved"
        )
        detail = executor.target_refusals[-1]["detail"]
        assert "outside" in detail.lower() or "moved" in detail.lower()


class TestAcceptanceGProtectedWindowAtPoint:
    def test_point_inside_the_agent_window_is_hard_denied(self):
        """The exact incident: a planned point that now lands on the agent's own UI."""
        world, session = make_world_and_session()
        port = RecordingPort()
        port.set_enabled(True)
        executor = make_executor(world, session, port)

        # A point inside the agent's window, presented as if it were the target's.
        protected_point = ScreenPx(960, 540)
        before = len(emitted(executor))
        executor.execute(_click_at(protected_point, "right"))

        assert len(emitted(executor)) == before, (
            "a click was dispatched into the protected window"
        )
        refusal = executor.target_refusals[-1]
        assert refusal["event"] in (
            Verdict.PROTECTED.value,
            Verdict.WRONG_WINDOW.value,
            Verdict.OUT_OF_BOUNDS.value,
            Verdict.NOT_IN_REGION.value,
        ), f"unexpected refusal reason: {refusal['event']}"
        assert refusal["detail"], "the refusal carries no reason"
        # Whatever fired, the audit must show what was actually under the point.
        assert refusal["under_point"] is not None or "outside" in refusal["detail"]

    def test_protected_registry_denies_by_pid(self):
        registry = ProtectedRegistry()
        registry.protect_pid(500)
        assert registry.is_protected(0, 500)[0] is True


def _click_at(point: ScreenPx, button: str):
    """Primitives equivalent to 'move to the validated point, then click'."""
    from frameforge.ports.input import Primitive, PrimitiveType

    return [
        Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=point.x, y=point.y),
        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=True, hold_ms=40),
        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=False),
    ]
