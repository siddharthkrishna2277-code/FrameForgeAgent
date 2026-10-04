"""Non-live simulation backend: records intent, never touches the OS.

Exists because a live run performed an unauthorized, account-level side effect. The lesson
was not "add another check" - it was that verifying a click target by *moving the real cursor*
to find out is itself the hazard, because the movement places a live pointer over whatever is
at that pixel.

So verification must be possible without movement. This backend records the exact planned
pointer and click sequence, computes what *would* be sent, and calls no OS input API at all.
A scenario can be rehearsed end to end - including the dual-monitor topology - with the
machine entirely untouched.

It is also the telemetry substrate: every field the re-enable checklist requires is captured
here, so the artifact is complete by construction rather than by remembering to log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from frameforge.ports.input import Primitive, PrimitiveType

#: Win32 SendInput flags, named. Recorded per event so a report can state exactly what would
#: have been emitted rather than asserting it in prose.
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001


@dataclass(frozen=True)
class VirtualScreen:
    """The four metrics a correct absolute mapping depends on.

    Kept as an explicit value type with no Win32 dependency, so a test can construct an
    arbitrary layout - including a negative origin, which is the case a size-only
    normalisation silently breaks - and reason about it without hardware.
    """

    x: int = 0
    y: int = 0
    width: int = 3840
    height: int = 1080

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def has_negative_origin(self) -> bool:
        return self.x < 0 or self.y < 0

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def monitor_of(self, px: int, py: int, monitors):
        """Which monitor contains this point.

        Monitor entries are ``(x0, y0, x1, y1)`` or ``(x0, y0, x1, y1, name)``. Returns the
        name when present, otherwise the rect, so telemetry is never empty.
        """
        for m in monitors:
            x0, y0, x1, y1 = m[0], m[1], m[2], m[3]
            if x0 <= px < x1 and y0 <= py < y1:
                return m[4] if len(m) > 4 else (x0, y0, x1, y1)
        return None

    def normalize(self, px: int, py: int) -> tuple[int, int]:
        """Physical virtual-desktop pixel -> normalised SendInput coordinate.

        The origin term is mandatory, not decorative. Dividing by the virtual *size* alone is
        arithmetically identical only while the origin is (0,0); with a negative origin every
        point is displaced by the origin, which on a two-monitor desktop is a whole monitor's
        width - so the click lands on the other display entirely.
        """
        span_x = max(1, self.width - 1)
        span_y = max(1, self.height - 1)
        nx = int((px - self.x) * 65535 / span_x)
        ny = int((py - self.y) * 65535 / span_y)
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def denormalize(self, nx: int, ny: int) -> tuple[int, int]:
        """Inverse of :meth:`normalize`, used by tests to prove round-trip fidelity."""
        span_x = max(1, self.width - 1)
        span_y = max(1, self.height - 1)
        return int(nx * span_x / 65535) + self.x, int(ny * span_y / 65535) + self.y


@dataclass
class SimulatedEvent:
    """One planned injection, with the full telemetry the re-enable checklist requires."""

    run_id: str
    action_id: str
    kind: str
    #: Intended target.
    target_hwnd: int = 0
    target_pid: int = 0
    target_title: str = ""
    target_class: str = ""
    #: Intended geometry, in each coordinate space named explicitly.
    client_point: tuple[int, int] | None = None
    screen_point: tuple[int, int] | None = None
    coordinate_space: str = "physical-virtual-desktop"
    virtual_desktop_bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    target_monitor_bounds: tuple[int, int, int, int] | None = None
    dpi_awareness: str = "per_monitor_v2"
    #: Computed payload - what *would* be sent.
    normalized_dx_dy: tuple[int, int] | None = None
    injection_flags: int = 0
    injection_flags_named: list[str] = field(default_factory=list)
    button: str = ""
    key: str = ""
    text: str = ""
    scancode: int = 0
    #: Decision trail.
    actual_cursor_point: tuple[int, int] | None = None
    actual_window: dict = field(default_factory=dict)
    foreground_window: dict = field(default_factory=dict)
    protected_target_decision: str = "not-evaluated"
    guard_decision: str = "not-evaluated"
    input_emitted: bool = False
    refusal_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "action_id": self.action_id,
            "kind": self.kind,
            "intended_target_hwnd": self.target_hwnd,
            "intended_target_pid": self.target_pid,
            "intended_target_title": self.target_title,
            "intended_target_class": self.target_class,
            "intended_client_point": self.client_point,
            "intended_screen_point": self.screen_point,
            "coordinate_space": self.coordinate_space,
            "virtual_desktop_bounds": self.virtual_desktop_bounds,
            "target_monitor_bounds": self.target_monitor_bounds,
            "dpi_awareness": self.dpi_awareness,
            "normalized_dx_dy": self.normalized_dx_dy,
            "injection_flags": self.injection_flags,
            "injection_flags_named": list(self.injection_flags_named),
            "button": self.button,
            "key": self.key,
            "text": self.text,
            "scancode": self.scancode,
            "actual_cursor_point": self.actual_cursor_point,
            "actual_window": dict(self.actual_window),
            "foreground_window": dict(self.foreground_window),
            "protected_target_decision": self.protected_target_decision,
            "guard_decision": self.guard_decision,
            "input_emitted": self.input_emitted,
            "refusal_reason": self.refusal_reason,
        }


#: Mouse flags, named. Values are per the Windows SDK (winuser.h), verified against the
#: installed headers rather than from memory.
_MOUSE_FLAG_NAMES: tuple[tuple[int, str], ...] = (
    (MOUSEEVENTF_MOVE, "MOUSEEVENTF_MOVE"),
    (MOUSEEVENTF_LEFTDOWN, "MOUSEEVENTF_LEFTDOWN"),
    (MOUSEEVENTF_LEFTUP, "MOUSEEVENTF_LEFTUP"),
    (MOUSEEVENTF_RIGHTDOWN, "MOUSEEVENTF_RIGHTDOWN"),
    (MOUSEEVENTF_RIGHTUP, "MOUSEEVENTF_RIGHTUP"),
    (MOUSEEVENTF_MIDDLEDOWN, "MOUSEEVENTF_MIDDLEDOWN"),
    (MOUSEEVENTF_MIDDLEUP, "MOUSEEVENTF_MIDDLEUP"),
    (MOUSEEVENTF_WHEEL, "MOUSEEVENTF_WHEEL"),
    (MOUSEEVENTF_ABSOLUTE, "MOUSEEVENTF_ABSOLUTE"),
    (MOUSEEVENTF_VIRTUALDESK, "MOUSEEVENTF_VIRTUALDESK"),
)

#: Keyboard flags, kept separate because MOUSEEVENTF_LEFTDOWN and KEYEVENTF_KEYUP are both
#: 0x0002 - a single combined table reports a left-button-down as a key-up.
_KEY_FLAG_NAMES: tuple[tuple[int, str], ...] = (
    (KEYEVENTF_EXTENDEDKEY, "KEYEVENTF_EXTENDEDKEY"),
    (KEYEVENTF_KEYUP, "KEYEVENTF_KEYUP"),
    (KEYEVENTF_UNICODE, "KEYEVENTF_UNICODE"),
    (KEYEVENTF_SCANCODE, "KEYEVENTF_SCANCODE"),
)


def mouse_flags_named(flags: int) -> list[str]:
    return [name for bit, name in _MOUSE_FLAG_NAMES if flags & bit]


def key_flags_named(flags: int) -> list[str]:
    return [name for bit, name in _KEY_FLAG_NAMES if flags & bit]


def flags_named(flags: int) -> list[str]:
    """Names for a mouse-shaped flag set. Keyboard events use :func:`key_flags_named`."""
    return mouse_flags_named(flags)


class SimulatedInputPort:
    """Records what would be sent. Calls no OS input API, ever.

    Satisfies the same surface as the live port so a runner cannot tell the difference - which
    is the point: a scenario rehearsed here is rehearsed through the real executor, policy and
    controller, not a parallel code path that might diverge.
    """

    name = "simulated"

    def __init__(self, virtual: VirtualScreen | None = None, *,
                 monitors: list[tuple] | None = None, run_id: str = "",
                 dpi_awareness: str = "per_monitor_v2") -> None:
        self.virtual = virtual or VirtualScreen()
        self.monitors = monitors or []
        self.run_id = run_id
        self.dpi_awareness = dpi_awareness
        self.events: list[SimulatedEvent] = []
        self._enabled = False
        self._action_seq = 0

    # -- live-port surface ------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool, *, thorough: bool = False) -> None:
        self._enabled = value

    def send(self, primitive: Primitive, *, action_id: str = "", source: str = "") -> bool:
        self._action_seq += 1
        aid = action_id or f"sim{self._action_seq:05d}"
        ev = SimulatedEvent(
            run_id=self.run_id,
            action_id=aid,
            kind=str(primitive.kind),
            virtual_desktop_bounds=(self.virtual.x, self.virtual.y,
                                    self.virtual.width, self.virtual.height),
            dpi_awareness=self.dpi_awareness,
        )

        if primitive.kind is PrimitiveType.MOUSE_MOVE_ABS:
            sx, sy = int(primitive.x), int(primitive.y)
            ev.screen_point = (sx, sy)
            ev.normalized_dx_dy = self.virtual.normalize(sx, sy)
            ev.injection_flags = (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                                  | MOUSEEVENTF_VIRTUALDESK)
            ev.target_monitor_bounds = self.virtual.monitor_of(sx, sy, self.monitors)
            # The decision a live run must make *without* moving anything.
            ev.guard_decision = (
                "in-bounds" if self.virtual.contains(sx, sy) else "OUT_OF_BOUNDS_REFUSE")
            if not ev.guard_decision.startswith("in-bounds"):
                ev.refusal_reason = "point outside the virtual desktop"
        elif primitive.kind is PrimitiveType.MOUSE_BUTTON:
            name = str(primitive.button or "left")
            ev.button = name
            down = bool(primitive.down)
            bit = {"left": MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP,
                   "right": MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP,
                   "middle": MOUSEEVENTF_MIDDLEDOWN if down else MOUSEEVENTF_MIDDLEUP,
                   }.get(name, 0)
            ev.injection_flags = bit
            # A button carries no point of its own; it acts where the cursor already is, which
            # is precisely why a button needs a prior verified move.
            ev.guard_decision = "requires-verified-cursor-position"
            if not down:
                ev.guard_decision = "release"
        elif primitive.kind is PrimitiveType.KEY:
            ev.key = str(primitive.key)
            ev.injection_flags = 0 if primitive.down else KEYEVENTF_KEYUP
            ev.injection_flags_named = key_flags_named(ev.injection_flags)
        elif primitive.kind is PrimitiveType.UNICODE:
            ev.text = primitive.text
            ev.injection_flags = KEYEVENTF_UNICODE
            ev.injection_flags_named = key_flags_named(ev.injection_flags)
        elif primitive.kind is PrimitiveType.SCANCODE:
            ev.scancode = int(primitive.scancode)
            ev.key = str(getattr(primitive, "scancode_key", "") or "")
            ev.injection_flags = (KEYEVENTF_SCANCODE
                                  if primitive.down else KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
            ev.injection_flags_named = key_flags_named(ev.injection_flags)
        elif primitive.kind is PrimitiveType.SCROLL:
            ev.injection_flags = MOUSEEVENTF_WHEEL
        elif primitive.kind is PrimitiveType.MOUSE_MOVE_REL:
            ev.injection_flags = MOUSEEVENTF_MOVE

        if not ev.injection_flags_named:
            ev.injection_flags_named = flags_named(ev.injection_flags)
        # Nothing is emitted. That is the entire point of this backend.
        ev.input_emitted = False
        ev.refusal_reason = "simulated-backend: no OS input is emitted"
        self.events.append(ev)
        return True

    def send_batch(self, primitives: list[Primitive], *, action_id: str = "") -> int:
        for p in primitives:
            self.send(p, action_id=action_id)
        return len(primitives)

    def send_raw_vk(self, vk: int, *, up: bool, armed: bool = True) -> bool:
        self._action_seq += 1
        self.events.append(SimulatedEvent(
            run_id=self.run_id, action_id=f"simvk{self._action_seq:05d}",
            kind="raw_vk", key=f"0x{vk:02X}",
            injection_flags=0 if up else 0,
            injection_flags_named=["KEYEVENTF_KEYUP"] if up else [],
            input_emitted=False,
            refusal_reason="simulated-backend: no OS input is emitted"))
        return True

    def release_all(self, *, thorough: bool = True) -> dict:
        return {"keys_released": [], "buttons_released": [], "mods_swept": [],
                "defensive_keys": [], "errors": [], "simulated": True}

    def cleanup(self) -> dict:
        return {"released": False}

    @property
    def sent_count(self) -> int:
        return 0

    # -- evidence ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "backend": self.name,
            "virtual_desktop": {
                "SM_XVIRTUALSCREEN": self.virtual.x,
                "SM_YVIRTUALSCREEN": self.virtual.y,
                "SM_CXVIRTUALSCREEN": self.virtual.width,
                "SM_CYVIRTUALSCREEN": self.virtual.height,
                "negative_origin": self.virtual.has_negative_origin,
            },
            "monitors": [list(m) for m in self.monitors],
            "dpi_awareness": self.dpi_awareness,
            "events": [e.to_dict() for e in self.events],
            "os_input_calls": 0,
        }

    def write(self, path: Path) -> Path:
        import json

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return path


__all__ = [
    "MOUSEEVENTF_ABSOLUTE",
    "key_flags_named",
    "mouse_flags_named",
    "MOUSEEVENTF_VIRTUALDESK",
    "SimulatedEvent",
    "SimulatedInputPort",
    "VirtualScreen",
    "flags_named",
]
