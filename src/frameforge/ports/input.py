"""Input port.

The single most safety-critical interface in the project. Two rules define it:

1. **Primitives, not actions.** An input port moves a mouse and presses keys. It has no
   concept of "click the Play button", no perception, and no idea what a game is. A
   logical ``Action`` becomes primitives only through the ActionCompiler, which is
   where the control profile, the display metrics and the blast-radius class are
   applied. This separation is what makes guardrail G-ROLE-02 testable.
2. **Every down has an up.** The executor owns hold lifetimes; the port exposes
   ``release_all`` because an emergency stop must be able to clear held state without
   reconstructing what was held.

Implementation is a single ``SendInput`` adapter (see
``adapters/input/sendinput.py``). PyAutoGUI is deliberately not used: no cp314 wheel,
no relative-mouse semantics, no reliable hold behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Protocol, runtime_checkable


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"
    X1 = "x1"
    X2 = "x2"


class Key(StrEnum):
    """Physical/logical keys by name.

    Names are language-neutral on purpose: the whole point of a ControlProfile is that
    a French keyboard layout can bind ``forward`` to ``Key.Z`` without any code change.
    The adapter maps names to virtual-key codes.
    """

    # letters and digits
    A = "a"
    B = "b"
    C = "c"
    D = "d"
    E = "e"
    F = "f"
    G = "g"
    H = "h"
    I = "i"
    J = "j"
    K = "k"
    L = "l"
    M = "m"
    N = "n"
    O = "o"
    P = "p"
    Q = "q"
    R = "r"
    S = "s"
    T = "t"
    U = "u"
    V = "v"
    W = "w"
    X = "x"
    Y = "y"
    Z = "z"
    N0 = "0"
    N1 = "1"
    N2 = "2"
    N3 = "3"
    N4 = "4"
    N5 = "5"
    N6 = "6"
    N7 = "7"
    N8 = "8"
    N9 = "9"

    # control
    SHIFT = "shift"
    LSHIFT = "lshift"
    RSHIFT = "rshift"
    CTRL = "ctrl"
    LCTRL = "lctrl"
    RCTRL = "rctrl"
    ALT = "alt"
    LALT = "lalt"
    RALT = "ralt"
    LWIN = "lwin"
    RWIN = "rwin"
    MENU = "menu"
    #: The dedicated Application key. Same virtual-key as MENU, but named for its actual
    #: function so a scenario author cannot reach it by accident.
    APPS = "apps"
    F10 = "f10"
    CAPSLOCK = "capslock"

    # navigation / editing
    SPACE = "space"
    ENTER = "enter"
    TAB = "tab"
    BACKSPACE = "backspace"
    DELETE = "delete"
    INSERT = "insert"
    HOME = "home"
    END = "end"
    PAGEUP = "pageup"
    PAGEDOWN = "pagedown"
    ESCAPE = "escape"
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"

    # punctuation
    MINUS = "minus"
    EQUALS = "equals"
    LBRACKET = "lbracket"
    RBRACKET = "rbracket"
    BACKSLASH = "backslash"
    SEMICOLON = "semicolon"
    APOSTROPHE = "apostrophe"
    COMMA = "comma"
    PERIOD = "period"
    SLASH = "slash"
    GRAVE = "grave"

    @classmethod
    def parse(cls, raw: str) -> Key:
        """Parse a key name, tolerating common aliases used in profiles."""
        s = raw.strip().lower().replace(" ", "").replace("_", "")
        table = {
            "esc": cls.ESCAPE,
            "return": cls.ENTER,
            "ctrl": cls.CTRL,
            "control": cls.CTRL,
            "shift": cls.SHIFT,
            "alt": cls.ALT,
            "win": cls.LWIN,
            "super": cls.LWIN,
            "meta": cls.LWIN,
            "del": cls.DELETE,
            "ins": cls.INSERT,
            "pgup": cls.PAGEUP,
            "pgdn": cls.PAGEDOWN,
            "spacebar": cls.SPACE,
            "bksp": cls.BACKSPACE,
        }
        if s in table:
            return table[s]
        try:
            return cls(s)
        except ValueError:
            msg = f"unknown key name: {raw!r}"
            raise ValueError(msg) from None


class PrimitiveType(StrEnum):
    """The complete set of things an input port can do.

    Small on purpose. Every entry is either necessary for "operate a game like a human"
    or is a future extension marked in the profile schema.
    """

    MOUSE_MOVE_ABS = "mouse_move_abs"
    MOUSE_MOVE_REL = "mouse_move_rel"
    MOUSE_BUTTON = "mouse_button"
    SCROLL = "scroll"
    KEY = "key"
    UNICODE = "unicode"
    GAMEPAD_STATE = "gamepad_state"  # P5+; feature-flagged, no-op when unsupported


@dataclass(frozen=True, slots=True)
class Primitive:
    """One indivisible input operation.

    ``hold_ms`` on a down-event is how the executor expresses "press and release after
    N ms" without a race between a timer thread and the main loop; the port performs
    the wait itself so ordering with subsequent primitives is exact.
    """

    kind: PrimitiveType
    x: int = 0
    y: int = 0
    dx: int = 0
    dy: int = 0
    button: MouseButton | None = None
    key: Key | None = None
    text: str = ""
    down: bool = False
    hold_ms: float = 0.0
    scroll_x: int = 0
    scroll_y: int = 0

    def describe(self) -> str:
        parts = [str(self.kind)]
        if self.kind in (PrimitiveType.MOUSE_MOVE_ABS,):
            parts.append(f"({self.x},{self.y})")
        elif self.kind == PrimitiveType.MOUSE_MOVE_REL:
            parts.append(f"(dx={self.dx},dy={self.dy})")
        elif self.kind == PrimitiveType.MOUSE_BUTTON:
            parts.append(f"{self.button} {'down' if self.down else 'up'}")
            if self.hold_ms:
                parts.append(f"{self.hold_ms:.0f}ms")
        elif self.kind == PrimitiveType.SCROLL:
            parts.append(f"({self.scroll_x},{self.scroll_y})")
        elif self.kind == PrimitiveType.KEY:
            parts.append(f"{self.key} {'down' if self.down else 'up'}")
            if self.hold_ms:
                parts.append(f"{self.hold_ms:.0f}ms")
        elif self.kind == PrimitiveType.UNICODE:
            parts.append(repr(self.text))
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class InputCapabilities:
    """What this machine/session can actually do. Reported by ``frameforge doctor``."""

    absolute_mouse: bool = False
    relative_mouse: bool = False
    mouse_buttons: int = 0
    scroll: bool = False
    unicode: bool = False
    gamepad: bool = False
    hotkeys: bool = True
    notes: tuple[str, ...] = ()


class ScrollUnit(IntEnum):
    """Scroll granularity."""

    WHEEL_CLICK = 120  # WHEEL_DELTA; what most games expect per notch


@runtime_checkable
class InputPort(Protocol):
    """A source of synthetic user-level input.

    Implementations MUST honour an ``enabled`` gate checked inside every send. The
    executor sets it false on estop, and no amount of queued work may then reach the
    OS (guardrail G-SES-01, stability criterion 4).
    """

    name: str

    @property
    def enabled(self) -> bool:
        """Whether input is currently permitted to reach the OS."""
        ...

    def set_enabled(self, value: bool) -> None:
        """Arm or disarm the port. Disarm must be immediate and total."""
        ...

    def send(self, primitive: Primitive) -> None:
        """Deliver one primitive. No-op when disabled."""
        ...

    def send_batch(self, primitives: list[Primitive]) -> int:
        """Deliver primitives in order. Returns how many were actually sent."""
        ...

    def release_all(self) -> None:
        """Release every held key and button. Must work even when disarmed."""
        ...

    def capabilities(self) -> InputCapabilities:
        ...

    def position(self) -> tuple[int, int]:
        """Current cursor position in absolute virtual-desktop coordinates."""
        ...


__all__ = [
    "InputCapabilities",
    "InputPort",
    "Key",
    "MouseButton",
    "Primitive",
    "PrimitiveType",
    "ScrollUnit",
]
