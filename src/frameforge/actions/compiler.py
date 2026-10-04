"""Action compiler: logical Action -> concrete primitives.

This is where a *control profile* and a *display profile* become input. It is the only
component that knows both vocabularies, and it is deliberately the only place a logical
action can become a primitive - which is what makes the "AI cannot emit input" property
structural rather than a convention (guardrail G-VAL-02, stability criterion 13).

Resolution order for every action:

1. Resolve ``Intent`` through the control profile. An unbound intent is a *compile
   error*, not a silent no-op: a profile that forgot to bind ``interact`` should fail
   loudly at load time, not produce a run that does nothing and reports UNKNOWN.
2. Resolve surface-local coordinates to absolute virtual-desktop pixels using the
   capture surface's offset. On this machine's 1920+1920 layout, skipping this is how a
   correct click lands on the wrong monitor.
3. Emit primitives in a fixed order with balanced hold semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from frameforge.actions.model import (
    AI_AUTHORABLE,
    Action,
    BlastClass,
    Click,
    Drag,
    GamepadAxis,
    GamepadButton,
    Hotkey,
    Intent,
    KeyDown,
    KeyPress,
    KeyUp,
    MouseButtonDown,
    MouseButtonUp,
    MouseLook,
    MoveMouse,
    Screenshot,
    Scroll,
    TypeText,
    Wait,
)
from frameforge.actions.executor import drag_primitives, hotkey_primitives, mouselook_primitives
from frameforge.kernel.errors import BlastRadiusError, SchemaValidationError
from frameforge.ports.capture import Surface
from frameforge.ports.geometry import Point, Size
from frameforge.ports.input import Key, MouseButton, Primitive, PrimitiveType


@dataclass(slots=True)
class Binding:
    """One control-profile binding: a logical intent to a physical input.

    ``kind`` is ``key`` (a tap/hold of one key), ``combo`` (a chord), ``mouse_button``,
    or ``axis`` (a stick, reserved for controller profiles).
    """

    kind: str
    keys: tuple[Key, ...] = ()
    button: MouseButton | None = None
    axis: str = ""
    value: float = 0.0
    hold_ms: int = 0

    def describe(self) -> str:
        if self.kind == "key" and self.keys:
            return f"key:{self.keys[0]}"
        if self.kind == "combo":
            return "+".join(str(k) for k in self.keys)
        if self.kind == "mouse_button":
            return f"mouse:{self.button}"
        if self.kind == "axis":
            return f"axis:{self.axis}={self.value}"
        return f"unbound({self.kind})"


@dataclass(slots=True)
class ControlProfile:
    """Logical-action vocabulary and its bindings for one control scheme.

    Ship presets: ``fps``, ``open_world``, ``racing``, ``rts``, ``sim_builder``,
    ``sandbox_navigation``. A new game picks a preset and overrides what differs, which
    is why the engine never needs a game name.
    """

    name: str
    bindings: dict[str, Binding] = None  # type: ignore[assignment]
    #: Mouse-look parameters. ``sensitivity`` scales relative motion; ``invert_y`` is a
    #: genuine per-user preference, not an evasion knob.
    look_sensitivity: float = 1.0
    look_steps: int = 12
    look_duration_ms: int = 120
    look_curve: str = "ease"
    #: Text-entry method for this surface: "scan" for games reading scancodes, "unicode"
    #: for ordinary UI.
    text_method: str = "scan"
    hold_ms_default: int = 60

    def __post_init__(self) -> None:
        if self.bindings is None:
            self.bindings = {}

    def bind(self, intent: str, binding: Binding) -> None:
        self.bindings[intent] = binding

    def resolve(self, intent: str) -> Binding | None:
        return self.bindings.get(intent)

    def bound_intents(self) -> tuple[str, ...]:
        return tuple(sorted(self.bindings))


@dataclass(slots=True)
class DisplayProfile:
    """Viewport geometry for the capture surface.

    ``viewport`` is a normalised region of the window that is the actual game view; the
    remainder may be letterbox, a launcher chrome strip, or a debug overlay. Coordinates
    are compiled against the *viewport*, so a profile authored for a 16:9 view works on a
    16:10 window without editing every ROI.
    """

    reference_size: Size
    viewport: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    def viewport_rect(self, surface_size: Size) -> "Rect":  # noqa: F821
        from frameforge.ports.geometry import Rect

        nx, ny, nw, nh = self.viewport
        return Rect.from_norm(nx, ny, nw, nh, surface_size)


@dataclass(slots=True)
class CompiledAction:
    """An action plus its compiled primitives and resolved blast class."""

    action: Action
    primitives: list[Primitive]
    blast: BlastClass
    expects_hold: bool = False
    note: str = ""

    def describe(self) -> str:
        n = len(self.primitives)
        return f"{self.action.describe()} -> {n} primitive{'s' if n != 1 else ''}"


class ActionCompiler:
    """Turns logical actions into ordered, balanced primitive batches."""

    def __init__(
        self,
        control: ControlProfile | None = None,
        display: DisplayProfile | None = None,
        *,
        surface: Surface | None = None,
        surface_size: Size | None = None,
        absolute_coords: bool = True,
    ) -> None:
        self.control = control or ControlProfile(name="default")
        self.display = display or DisplayProfile(reference_size=Size(1920, 1080))
        self.surface = surface
        self.surface_size = surface_size or (surface.size if surface else self.display.reference_size)
        self.absolute_coords = absolute_coords

    # ------------------------------------------------------------------- geometry

    def to_absolute(self, at: Point) -> Point:
        """Surface-local -> absolute virtual-desktop pixels.

        Applies the surface offset so a click on the second monitor lands on the second
        monitor. This is the single most important line in the compiler for a multi-monitor
        user, and the reason ``Surface.offset_x`` exists at all.
        """
        if not self.absolute_coords or self.surface is None:
            return at
        sx, sy = self.surface.to_screen(at.x, at.y)
        return Point(sx, sy)

    # -------------------------------------------------------------------- compile

    def compile(self, action: Action) -> CompiledAction:
        """Compile one action.

        Raises ``SchemaValidationError`` for an unbound intent and ``BlastRadiusError``
        for a forbidden class. Both are compile-time failures, so a profile mistake is
        caught before anything is sent.
        """
        handler = getattr(self, f"_compile_{action.type}", None)
        if handler is None:
            msg = f"no compiler for action type {action.type!r}"
            raise SchemaValidationError(msg)
        return handler(action)

    def compile_batch(self, actions: list[Action]) -> list[CompiledAction]:
        return [self.compile(a) for a in actions]

    # ------------------------------------------------------------- action kinds

    def _compile_wait(self, a: Wait) -> CompiledAction:
        return CompiledAction(a, [], BlastClass.OBSERVE, note=f"sleep {a.ms}ms")

    def _compile_screenshot(self, a: Screenshot) -> CompiledAction:
        return CompiledAction(a, [], BlastClass.OBSERVE, note=f"evidence tag={a.tag}")

    def _compile_intent(self, a: Intent) -> CompiledAction:
        binding = self.control.resolve(a.intent)
        if binding is None:
            known = ", ".join(self.control.bound_intents()[:12])
            msg = (
                f"intent {a.intent!r} is not bound in control profile "
                f"{self.control.name!r}; bound intents: {known}"
            )
            raise SchemaValidationError(msg)
        if binding.kind == "key" and binding.keys:
            return self._key_from_binding(a, binding)
        if binding.kind == "combo":
            return CompiledAction(
                a,
                hotkey_primitives(list(binding.keys), self.control.hold_ms_default),
                a.blast(),
            )
        if binding.kind == "mouse_button" and binding.button:
            prims = [
                Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=binding.button, down=True,
                          hold_ms=max(a.hold_ms, binding.hold_ms, self.control.hold_ms_default)),
                Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=binding.button, down=False),
            ]
            return CompiledAction(a, prims, a.blast())
        if binding.kind == "axis":
            return CompiledAction(
                a,
                [Primitive(kind=PrimitiveType.GAMEPAD_STATE, axis=binding.axis,
                           dy=int(binding.value * 1000), hold_ms=a.hold_ms)],
                BlastClass.SUSTAINED,
            )
        msg = f"binding for {a.intent!r} is malformed: {binding.describe()}"
        raise SchemaValidationError(msg)

    def _key_from_binding(self, a: Intent, binding: Binding) -> CompiledAction:
        key = binding.keys[0]
        hold = max(a.hold_ms, binding.hold_ms, self.control.hold_ms_default)
        prims: list[Primitive] = []
        for _ in range(max(1, a.count)):
            prims.append(Primitive(kind=PrimitiveType.KEY, key=key, down=True, hold_ms=hold))
            prims.append(Primitive(kind=PrimitiveType.KEY, key=key, down=False))
        return CompiledAction(a, prims, BlastClass.REVERSIBLE)

    def _compile_move_mouse(self, a: MoveMouse) -> CompiledAction:
        """Absolute pointer placement.

        ``relative`` is accepted on the model for call-site symmetry but the offset is
        carried by ``MouseLook``, which is the action with real relative semantics. A
        relative move here would be ambiguous about its origin.
        """
        abs_pt = self.to_absolute(a.at)
        per_step = a.duration_ms / max(1, a.steps)
        if a.steps > 1 and a.duration_ms > 0:
            prims = [
                Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y,
                          hold_ms=per_step)
                for _ in range(a.steps)
            ]
        else:
            prims = [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y)]
        return CompiledAction(a, prims, a.blast())

    def _require_point(self, a: object, what: str) -> None:
        """Refuse any mouse-button action that has no validated point.

        Defence in depth behind the schema: the schema requires a point, and the compiler
        refuses one anyway. An action reaching the compiler without a point used to compile
        to a bare button press, which lands wherever the operator's cursor is.
        """
        point = getattr(a, "at", None)
        if point is None:
            msg = (
                f"{what} has no target point. Every mouse-button action must name a point "
                "inside the registered target; an action without one would act at the "
                "operator's current cursor position. Use CursorClick if a "
                "cursor-relative action is genuinely intended."
            )
            raise SchemaValidationError(msg)

    def _compile_click(self, a: Click) -> CompiledAction:
        self._require_point(a, "Click")
        prims: list[Primitive] = []
        abs_pt = self.to_absolute(a.at)
        prims.append(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y))
        for _ in range(a.count):
            prims.append(
                Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button, down=True,
                          hold_ms=a.hold_ms or 40)
            )
            prims.append(Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button, down=False))
        return CompiledAction(a, prims, a.blast())

    def _compile_mouse_down(self, a: MouseButtonDown) -> CompiledAction:
        self._require_point(a, "MouseButtonDown")
        abs_pt = self.to_absolute(a.at)
        return CompiledAction(
            a,
            [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y),
             Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button, down=True)],
            a.blast(), expects_hold=True,
        )

    def _compile_mouse_up(self, a: MouseButtonUp) -> CompiledAction:
        self._require_point(a, "MouseButtonUp")
        abs_pt = self.to_absolute(a.at)
        return CompiledAction(
            a,
            [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y),
             Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button, down=False)],
            a.blast(),
        )

    def _compile_drag(self, a: Drag) -> CompiledAction:
        path = [self.to_absolute(p) for p in a.path]
        return CompiledAction(a, drag_primitives(path, a.button, a.duration_ms), a.blast())

    def _compile_cursor_click(self, a) -> CompiledAction:
        """Cursor-relative click: no move, so it acts where the cursor already is.

        This is the only action permitted to act at the cursor, and it must name the
        session that authorises it. ``TargetGuard`` re-checks the window under the cursor
        immediately before the button goes down, so a cursor that has drifted onto an
        unrelated window is caught rather than trusted.
        """
        if not a.require_session:
            msg = (
                "CursorClick requires require_session: the registered target the cursor "
                "must already be inside. Without it, a cursor-relative click would act on "
                "whatever happens to be under the pointer."
            )
            raise SchemaValidationError(msg)
        prims: list[Primitive] = []
        for _ in range(a.count):
            prims.append(Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button,
                                    down=True, hold_ms=a.hold_ms or 40))
            prims.append(Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=a.button,
                                    down=False))
        return CompiledAction(a, prims, a.blast(),
                              note=f"cursor-relative, session={a.require_session}")

    def _compile_scroll(self, a: Scroll) -> CompiledAction:
        prims: list[Primitive] = []
        if a.at is not None:
            abs_pt = self.to_absolute(a.at)
            prims.append(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=abs_pt.x, y=abs_pt.y))
        per_step = a.dx // max(1, a.steps), a.dy // max(1, a.steps)
        for _ in range(a.steps):
            prims.append(
                Primitive(kind=PrimitiveType.SCROLL, scroll_x=per_step[0], scroll_y=per_step[1],
                          hold_ms=a.duration_ms / max(1, a.steps))
            )
        return CompiledAction(a, prims, a.blast())

    def _compile_mouse_look(self, a: MouseLook) -> CompiledAction:
        dx = int(a.dx * self.control.look_sensitivity)
        dy = int(a.dy * self.control.look_sensitivity)
        prims = mouselook_primitives(dx, dy, a.steps, a.duration_ms, a.curve)
        return CompiledAction(a, prims, a.blast())

    def _compile_key_press(self, a: KeyPress) -> CompiledAction:
        prims: list[Primitive] = []
        for _ in range(a.count):
            prims.append(Primitive(kind=PrimitiveType.KEY, key=a.key, down=True, hold_ms=a.hold_ms))
            prims.append(Primitive(kind=PrimitiveType.KEY, key=a.key, down=False))
        return CompiledAction(a, prims, a.blast())

    def _compile_key_down(self, a: KeyDown) -> CompiledAction:
        return CompiledAction(
            a, [Primitive(kind=PrimitiveType.KEY, key=a.key, down=True)],
            a.blast(), expects_hold=True,
        )

    def _compile_key_up(self, a: KeyUp) -> CompiledAction:
        return CompiledAction(
            a, [Primitive(kind=PrimitiveType.KEY, key=a.key, down=False)], a.blast()
        )

    def _compile_hotkey(self, a: Hotkey) -> CompiledAction:
        """Compile a chord, refusing input-language switching combinations.

        This is the only place a chord is knowable. Individual key events cannot know
        they will become a chord - Ctrl pressed alone is ordinary - so the check belongs
        where the whole key set is named at once. A profile that bound Ctrl+Shift to an
        intent would otherwise stream the first modifier out and be unable to stop.
        """
        from frameforge.actions.safety import InputSafetyManager

        violation, detail = InputSafetyManager().check_chord([str(k) for k in a.keys])
        if violation.value != "none":
            msg = (
                f"hotkey {'+'.join(str(k) for k in a.keys)} refused: {detail}. "
                "Input-language switching combinations are not permitted "
                "(docs/AI_GUARDRAILS.md G-ABS-10)."
            )
            raise BlastRadiusError(msg)
        return CompiledAction(a, hotkey_primitives(a.keys, a.hold_ms), a.blast())

    def _compile_type_text(self, a: TypeText) -> CompiledAction:
        if a.method == "unicode":
            return CompiledAction(
                a,
                [Primitive(kind=PrimitiveType.UNICODE, text=a.text)],
                a.blast(),
            )
        # Scan mode: emit a real hardware scancode (KEYEVENTF_SCANCODE), which is the only
        # thing a game reading raw WM_INPUT / DirectInput scancodes will accept.
        #
        # The previous implementation here expanded the text to Key values and emitted
        # ordinary virtual-key events with wScan=0. That is *identical* to unicode mode, so
        # 'scan' was a label that described a capability the code did not have - and a
        # profile selecting it for a scancode-reading target got silent no-ops.
        from frameforge.actions.textmap import text_to_keys

        prims: list[Primitive] = []
        for key in text_to_keys(a.text):
            scancode = key.scancode
            if scancode is None:
                msg = (f"key {key.name} has no scancode; it cannot be typed in scan mode. "
                       "Use method='unicode', or bind an intent.")
                raise ValueError(msg)
            prims.append(
                Primitive(kind=PrimitiveType.SCANCODE, scancode=scancode,
                          scancode_key=key, down=True,
                          hold_ms=a.interval_ms or 12))
            prims.append(
                Primitive(kind=PrimitiveType.SCANCODE, scancode=scancode,
                          scancode_key=key, down=False))
        return CompiledAction(a, prims, a.blast())

    def _compile_gamepad_button(self, a: GamepadButton) -> CompiledAction:
        return CompiledAction(
            a,
            [Primitive(kind=PrimitiveType.GAMEPAD_STATE, text=a.button, down=a.down,
                       hold_ms=a.hold_ms)],
            a.blast(), expects_hold=a.down,
        )

    def _compile_gamepad_axis(self, a: GamepadAxis) -> CompiledAction:
        return CompiledAction(
            a,
            [Primitive(kind=PrimitiveType.GAMEPAD_STATE, text=a.axis, dy=int(a.value * 1000),
                       hold_ms=a.duration_ms)],
            a.blast(),
        )


def check_blast_allowed(blast: BlastClass, *, ai_authored: bool, allow_irreversible: bool) -> None:
    """Enforce the blast-radius rules.

    Applied to the *action*, not the author, so an AI-proposed C3 action is rejected
    exactly like a human-proposed one (guardrail G-BLAST-01). ``C4`` is never allowed.
    """
    if blast is BlastClass.FORBIDDEN:
        msg = f"action class {blast} is forbidden unconditionally"
        raise BlastRadiusError(msg)
    if blast is BlastClass.EXTERNAL and not allow_irreversible:
        msg = (
            "external-effect action requires allow_irreversible=true in the scenario; "
            "see docs/AI_GUARDRAILS.md G-BLAST-01/G-BLAST-02"
        )
        raise BlastRadiusError(msg)
    if ai_authored and blast not in AI_AUTHORABLE:
        msg = f"AI may not author {blast} actions"
        raise BlastRadiusError(msg)


__all__ = [
    "ActionCompiler",
    "Binding",
    "CompiledAction",
    "ControlProfile",
    "DisplayProfile",
    "check_blast_allowed",
]
