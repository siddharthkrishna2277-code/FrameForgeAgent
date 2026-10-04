"""Logical action model.

An ``Action`` is what a task, a profile, or an AI planner *asks for*. It is never
executed directly. The pipeline is always:

    Action -> ActionCompiler -> [Primitive, ...] -> validator -> executor -> OS

That indirection is the whole safety story. A planner cannot emit a primitive, so it
cannot bypass the control profile, the blast-radius class, the budget ledger, or the
focus guard - all of which sit between the action and the OS.

Actions are engine-agnostic by construction: they name *intents* (``forward``,
``interact``, ``confirm``) or generic mechanics (``click``, ``hotkey``). They never
name a key or a coordinate that is not expressed relative to a captured surface. The
core contains no game-specific values (guardrail G-DEV-01).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from frameforge.ports.geometry import Point
from frameforge.ports.input import Key, MouseButton


class BlastClass(StrEnum):
    """How much damage this action can do if the reasoning behind it is wrong.

    Enforced on the *action*, not on the author: an AI-proposed C3 action is rejected
    exactly like a human-proposed one (guardrail G-BLAST-01).
    """

    OBSERVE = "c0_observe"
    REVERSIBLE = "c1_reversible"
    SUSTAINED = "c2_sustained"
    EXTERNAL = "c3_external"
    FORBIDDEN = "c4_forbidden"


#: Actions the AI planner may author at all. C3 is human/profile-authored only, and
#: the validator enforces this on the action itself rather than trusting provenance.
AI_AUTHORABLE: frozenset[BlastClass] = frozenset(
    {BlastClass.OBSERVE, BlastClass.REVERSIBLE, BlastClass.SUSTAINED}
)


class ActionBase(BaseModel):
    """Common fields for every action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Overrides the compiler's default blast class. Only ever *lowers* it; raising it
    #: is rejected by the validator (guardrail G-DEC-03: no self-elevation).
    blast_class: BlastClass | None = None
    label: str = Field(default="", max_length=60)

    def blast(self) -> BlastClass:
        return self.blast_class or self.default_blast()

    def default_blast(self) -> BlastClass:
        raise NotImplementedError

    def describe(self) -> str:
        return self.__class__.__name__


# --------------------------------------------------------------------- mouse


class MoveMouse(ActionBase):
    """Move the pointer to an absolute point.

    Coordinates are *surface-local* and resolved against the capture surface by the
    compiler, which adds the surface's virtual-desktop offset. Storing them surface-local
    is what makes a profile authored on one monitor work on the second monitor.
    """

    type: Literal["move_mouse"] = "move_mouse"
    at: Point
    relative: bool = False
    duration_ms: int = Field(default=0, ge=0, le=5000)
    steps: int = Field(default=1, ge=1, le=200)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class Click(ActionBase):
    """Press and release a mouse button **at a validated point**.

    ``at`` is required. It was optional, and an optional point meant the compiler emitted
    no move and the button landed wherever the operator's physical cursor happened to be -
    which is how a click can land in an unrelated window while every coordinate
    calculation in the system is correct. Correct movement does not prove correct
    authorisation, so the un-anchored form is removed rather than deprecated.

    Use :class:`CursorClick` if you genuinely need cursor-relative behaviour; it requires an
    explicit anchor and goes through the same target validation.
    """

    type: Literal["click"] = "click"
    at: Point
    button: MouseButton = MouseButton.LEFT
    count: int = Field(default=1, ge=1, le=5)
    hold_ms: int = Field(default=0, ge=0, le=10_000)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class MouseButtonDown(ActionBase):
    """Press without releasing, at a validated point. Requires a matching up.

    ``at`` is required for the same reason as :class:`Click`.
    """

    type: Literal["mouse_down"] = "mouse_down"
    at: Point
    button: MouseButton = MouseButton.LEFT

    def default_blast(self) -> BlastClass:
        return BlastClass.SUSTAINED


class MouseButtonUp(ActionBase):
    """Release a button. Always safe; part of the recovery surface.

    ``at`` is required even for a release: a release without a point would still send an
    absolute move, and a defensive release must never move the operator's cursor.
    """

    type: Literal["mouse_up"] = "mouse_up"
    at: Point
    button: MouseButton = MouseButton.LEFT

    def default_blast(self) -> BlastClass:
        return BlastClass.OBSERVE


class CursorClick(ActionBase):
    """Click at the cursor's *current* position, explicitly.

    Exists because removing the implicit form would otherwise leave no way to express a
    deliberate cursor-relative action. It requires an anchor naming the registered target
    the cursor must already be inside, and it is validated by
    :class:`~frameforge.actions.target.TargetGuard` exactly like every other mouse action.
    Nothing can act at the cursor without saying which target that is allowed to be.
    """

    type: Literal["cursor_click"] = "cursor_click"
    #: Name of the registered target session the cursor must be inside.
    require_session: str = ""
    button: MouseButton = MouseButton.LEFT
    count: int = Field(default=1, ge=1, le=5)
    hold_ms: int = Field(default=40, ge=0, le=5000)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE

    def describe(self) -> str:
        return f"cursor_click({self.button}, requires {self.require_session!r})"


class Drag(ActionBase):
    """Press at a point, move along a path, release.

    Used for RTS box-select, camera panning, inventory drags, and slider adjustments.
    """

    type: Literal["drag"] = "drag"
    path: list[Point] = Field(min_length=2, max_length=200)
    button: MouseButton = MouseButton.LEFT
    duration_ms: int = Field(default=600, ge=0, le=30_000)

    def default_blast(self) -> BlastClass:
        return BlastClass.SUSTAINED


class Scroll(ActionBase):
    """Wheel scroll, optionally at a point."""

    type: Literal["scroll"] = "scroll"
    dx: int = Field(default=0, ge=-2000, le=2000)
    dy: int = Field(default=0, ge=-2000, le=2000)
    at: Point | None = None
    steps: int = Field(default=1, ge=1, le=100)
    duration_ms: int = Field(default=0, ge=0, le=5000)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class MouseLook(ActionBase):
    """Relative mouse motion - the mouse-look mechanic.

    Emitted as N relative sub-moves so it is interruptible mid-motion (an estop during a
    flick must stop the flick) and so the motion has a shape the game can distinguish
    from a teleport, which matters for games with input smoothing.

    ``curve`` exists for game mechanics, not for evasion: a real mouse arc is smoother
    than a linear interpolation, and some games' aim smoothing depends on the timing
    profile. It is not used to defeat behavioural detection (guardrail G-ABS-03).
    """

    type: Literal["mouse_look"] = "mouse_look"
    dx: int
    dy: int
    steps: int = Field(default=12, ge=1, le=200)
    duration_ms: int = Field(default=120, ge=0, le=10_000)
    curve: Literal["linear", "ease"] = "ease"

    def default_blast(self) -> BlastClass:
        return BlastClass.SUSTAINED


# ------------------------------------------------------------------- keyboard


class KeyPress(ActionBase):
    """Tap a key."""

    type: Literal["key_press"] = "key_press"
    key: Key
    hold_ms: int = Field(default=30, ge=1, le=5000)
    count: int = Field(default=1, ge=1, le=20)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class KeyDown(ActionBase):
    """Hold a key down. Must be matched by a ``KeyUp`` before the step boundary."""

    type: Literal["key_down"] = "key_down"
    key: Key

    def default_blast(self) -> BlastClass:
        return BlastClass.SUSTAINED


class KeyUp(ActionBase):
    """Release a key."""

    type: Literal["key_up"] = "key_up"
    key: Key

    def default_blast(self) -> BlastClass:
        return BlastClass.OBSERVE


class Hotkey(ActionBase):
    """Press a key combination in order, then release in reverse order.

    Reverse-order release matters: games that track modifier state get confused by a
    forward-order release.
    """

    type: Literal["hotkey"] = "hotkey"
    keys: list[Key] = Field(min_length=1, max_length=5)
    hold_ms: int = Field(default=50, ge=1, le=2000)

    @field_validator("keys")
    @classmethod
    def _no_repeat(cls, v: list[Key]) -> list[Key]:
        if len(set(v)) != len(v):
            msg = f"duplicate keys in hotkey: {v}"
            raise ValueError(msg)
        return v

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class TypeText(ActionBase):
    """Type literal text.

    Two methods, and the choice matters: ``unicode`` posts WM_CHAR-equivalent unicode
    events (works for most UI, wrong for games reading raw scancodes), while ``scan``
    uses scancodes and extended flags (works for games). Default is ``scan`` for game
    surfaces, configurable per control profile.
    """

    type: Literal["type_text"] = "type_text"
    text: str = Field(min_length=1, max_length=500)
    method: Literal["unicode", "scan"] = "scan"
    #: Gap between characters.
    #:
    #: 12 ms was too tight for a real editor: the first live POC delivered all 12
    #: characters of "FFPROBE7421X" to a correctly-identified, correctly-focused Notepad
    #: and the buffer received only part of it. The characters reached the OS - the audit
    #: shows 12 dispatches, 12 deliveries, 0 violations - and the target discarded them.
    #: 35 ms is the smallest gap that survived measurement against Notepad on this
    #: hardware, and it is a floor for human-plausible typing rather than a speed target:
    #: anything faster risks the same silent loss, and a dropped character is
    #: indistinguishable from a verifier failure at the report level.
    interval_ms: int = Field(default=35, ge=0, le=500)

    def default_blast(self) -> BlastClass:
        return BlastClass.EXTERNAL  # text lands somewhere; may hit a text field

    def describe(self) -> str:
        return f"type_text({len(self.text)} chars, {self.method})"


class Wait(ActionBase):
    """An explicit, rate-limited wait.

    Exists as a first-class action so that "wait for the loading screen" is visible,
    budgeted, and verifiable - rather than the ``sleep(N); assume success`` pattern
    this whole system is built to avoid. Every wait must be paired with a
    postcondition.
    """

    type: Literal["wait"] = "wait"
    ms: int = Field(default=500, ge=0, le=600_000)

    def default_blast(self) -> BlastClass:
        return BlastClass.OBSERVE

    def describe(self) -> str:
        return f"wait({self.ms}ms)"


class Screenshot(ActionBase):
    """Explicitly capture evidence now.

    Modelled as an action rather than a flag so it participates in budgets, appears in
    the plan trace, and can be required by a scenario's evidence policy.
    """

    type: Literal["screenshot"] = "screenshot"
    tag: str = Field(default="", max_length=60)

    def default_blast(self) -> BlastClass:
        return BlastClass.OBSERVE

    def describe(self) -> str:
        return f"screenshot({self.tag or 'untagged'})"


# -------------------------------------------------------------------- gamepad


class GamepadButton(ActionBase):
    """Press or release a controller button. P5+, feature-flagged."""

    type: Literal["gamepad_button"] = "gamepad_button"
    button: str = Field(pattern=r"^[a-z0-9_]{1,24}$")
    down: bool = True
    hold_ms: int = Field(default=0, ge=0, le=10_000)

    def default_blast(self) -> BlastClass:
        return BlastClass.REVERSIBLE


class GamepadAxis(ActionBase):
    """Move a stick/trigger axis. P5+, feature-flagged."""

    type: Literal["gamepad_axis"] = "gamepad_axis"
    axis: str = Field(pattern=r"^(left_x|left_y|right_x|right_y|lt|rt)$")
    value: float = Field(ge=-1.0, le=1.0)
    duration_ms: int = Field(default=0, ge=0, le=10_000)

    def default_blast(self) -> BlastClass:
        return BlastClass.SUSTAINED


# ------------------------------------------------------------------- intent

#: Semantic intents a control profile binds to physical keys/buttons. The core knows
#: these names as a vocabulary; it has no idea what they are bound to. This is the
#: mechanism by which one engine serves FPS, open-world, racing, RTS, sim and sandbox
#: titles without any of them appearing in the code.
KNOWN_INTENTS: tuple[str, ...] = (
    "forward", "backward", "left", "right", "strafe_left", "strafe_right",
    "jump", "crouch", "prone", "sprint", "walk", "dodge", "roll",
    "interact", "use", "pickup", "inventory", "map", "journal", "quests",
    "reload", "fire", "aim", "melee", "weapon_next", "weapon_prev", "grenade",
    "accelerate", "brake", "steer_left", "steer_right", "handbrake", "camera_left",
    "camera_right", "select_all", "deselect", "unit_place", "building_menu",
    "place_block", "rotate", "zoom_in", "zoom_out", "pan_up", "pan_down",
    "pan_left", "pan_right", "hotbar_1", "hotbar_2", "hotbar_3", "hotbar_4",
    "craft", "eat", "sleep", "build", "confirm", "cancel", "back", "menu",
    "pause", "tab", "restart", "debug_overlay",
)


class Intent(ActionBase):
    """Perform a semantic intent, resolved through the active ControlProfile.

    This is the action that makes Frame Forge game-agnostic: ``Intent("jump")`` compiles
    to Space, or to the gamepad south face button, or to whatever the profile says -
    without any code change and without any game named anywhere.
    """

    type: Literal["intent"] = "intent"
    intent: str
    hold_ms: int = Field(default=0, ge=0, le=10_000)
    #: Optional repeat count, e.g. tapping a hotbar slot.
    count: int = Field(default=1, ge=1, le=50)

    @field_validator("intent")
    @classmethod
    def _known_or_custom(cls, v: str) -> str:
        s = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{1,40}", s):
            msg = f"invalid intent name: {v!r}"
            raise ValueError(msg)
        return s

    def default_blast(self) -> BlastClass:
        # An intent resolves through the profile; until it does, assume it could be
        # sustained movement. The compiler downgrades this when it resolves to a tap.
        return BlastClass.SUSTAINED

    def describe(self) -> str:
        return f"intent({self.intent})"


Action = Annotated[
    Union[
        MoveMouse, Click, CursorClick, MouseButtonDown, MouseButtonUp, Drag,
        Scroll, MouseLook,
        KeyPress, KeyDown, KeyUp, Hotkey, TypeText, Wait, Screenshot,
        GamepadButton, GamepadAxis, Intent,
    ],
    Field(discriminator="type"),
]


class HoldTracker:
    """Tracks which keys/buttons are currently held, so nothing is left stuck.

    The executor holds one of these. Its real job is the invariant in
    ``assert_balanced``: at any step boundary, held state must be empty. A leaked hold
    is the single nastiest failure this system can have, because it silently keeps a
    key pressed while the agent believes it has stopped - the user's next keystroke
    then arrives in a modified state.

    Kept deliberately simple and dumb, with no timers: it mirrors what was actually
    sent.
    """

    __slots__ = ("_keys", "_buttons", "_opened_at")

    def __init__(self) -> None:
        self._keys: set[Key] = set()
        self._buttons: set[MouseButton] = set()
        self._opened_at: dict[str, float] = {}

    def key_down(self, key: Key, mono_ms: float) -> None:
        self._keys.add(key)
        self._opened_at[f"key:{key}"] = mono_ms

    def key_up(self, key: Key, mono_ms: float) -> None:
        self._keys.discard(key)
        self._opened_at.pop(f"key:{key}", None)

    def button_down(self, button: MouseButton, mono_ms: float) -> None:
        self._buttons.add(button)
        self._opened_at[f"btn:{button}"] = mono_ms

    def button_up(self, button: MouseButton, mono_ms: float) -> None:
        self._buttons.discard(button)
        self._opened_at.pop(f"btn:{button}", None)

    @property
    def held_keys(self) -> set[Key]:
        return set(self._keys)

    @property
    def held_buttons(self) -> set[MouseButton]:
        return set(self._buttons)

    @property
    def held_count(self) -> int:
        return len(self._keys) + len(self._buttons)

    def held_mono_ms(self, mono_ms: float) -> dict[str, float]:
        return {name: round(mono_ms - started, 1) for name, started in self._opened_at.items()}

    def assert_balanced(self, mono_ms: float = 0.0) -> None:
        """Raise if anything is still held. Called at every step boundary."""
        if self.held_count:
            stuck = ", ".join(sorted(self.held_mono_ms(mono_ms)))
            msg = f"unbalanced hold state at boundary: {stuck}"
            raise RuntimeError(msg)

    def release_plan(self) -> list[tuple[str, object]]:
        """Ordered release list. Keys before buttons, insertion-order within each.

        Insertion order matters: releasing in the reverse of press order is what a
        driver does, and some games care about modifier nesting.
        """
        out: list[tuple[str, object]] = [(f"key:{k}", k) for k in self._keys]
        out += [(f"btn:{b}", b) for b in self._buttons]
        return out


__all__ = [
    "AI_AUTHORABLE",
    "KNOWN_INTENTS",
    "Action",
    "ActionBase",
    "BlastClass",
    "Click",
    "Drag",
    "GamepadAxis",
    "GamepadButton",
    "HoldTracker",
    "Hotkey",
    "Intent",
    "KeyDown",
    "KeyPress",
    "KeyUp",
    "MouseButtonDown",
    "MouseButtonUp",
    "MouseLook",
    "MoveMouse",
    "Screenshot",
    "Scroll",
    "TypeText",
    "Wait",
]
