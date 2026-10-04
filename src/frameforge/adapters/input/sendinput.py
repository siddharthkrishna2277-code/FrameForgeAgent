"""Win32 ``SendInput`` adapter - a thin guarded facade over ``InputSafetyManager``.

Why raw ``SendInput`` rather than a convenience library: one ordered stream, absolute
*and* relative mouse motion, hold/release lifetimes, and no extra dependency. PyAutoGUI
was rejected - no cp314 wheel, no relative-mouse or hold semantics, and an extra failure
mode.

**This class no longer injects anything itself.** All injection, held-key tracking and
input-state hygiene belong to :class:`frameforge.actions.safety.InputSafetyManager`,
which is the only code path permitted to synthesise input anywhere in the project. That
is not tidiness; it is the fix for a release-blocking defect:

    The previous version injected keys here without recording what it held, and refused to
    deliver a key-*up* once the port was disarmed. An emergency stop that fired in the
    middle of a Ctrl+Alt chord therefore disarmed the port and then blocked the releases -
    leaving Ctrl and Alt logically held on the operator's machine. Every physical keypress
    afterwards behaved as a chord (an audio tool cycling outputs on Ctrl+Shift, ``N``
    producing an alternate character, Backspace and Delete dead), recoverable only by
    restarting Windows.

Two rules are encoded below and are load-bearing:

1. A **press** is refused while disarmed.
2. A **release is never refused.** Blocking a release is precisely what strands a key.
"""

from __future__ import annotations

from ctypes import wintypes

from frameforge.actions.safety import InputSafetyManager
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.ports.geometry import Point, Size
from frameforge.ports.input import InputCapabilities, Key, MouseButton, Primitive, PrimitiveType

#: Authoritative name -> virtual-key code table. The safety manager imports this, so the
#: dispatcher and the release path can never drift apart.
_VK: dict[Key, int] = {
    Key.A: 0x41, Key.B: 0x42, Key.C: 0x43, Key.D: 0x44, Key.E: 0x45, Key.F: 0x46,
    Key.G: 0x47, Key.H: 0x48, Key.I: 0x49, Key.J: 0x4A, Key.K: 0x4B, Key.L: 0x4C,
    Key.M: 0x4D, Key.N: 0x4E, Key.O: 0x4F, Key.P: 0x50, Key.Q: 0x51, Key.R: 0x52,
    Key.S: 0x53, Key.T: 0x54, Key.U: 0x55, Key.V: 0x56, Key.W: 0x57, Key.X: 0x58,
    Key.Y: 0x59, Key.Z: 0x5A,
    Key.N0: 0x30, Key.N1: 0x31, Key.N2: 0x32, Key.N3: 0x33, Key.N4: 0x34,
    Key.N5: 0x35, Key.N6: 0x36, Key.N7: 0x37, Key.N8: 0x38, Key.N9: 0x39,
    Key.SHIFT: 0x10, Key.LSHIFT: 0xA0, Key.RSHIFT: 0xA1,
    Key.CTRL: 0x11, Key.LCTRL: 0xA2, Key.RCTRL: 0xA3,
    Key.ALT: 0x12, Key.LALT: 0xA4, Key.RALT: 0xA5,
    Key.LWIN: 0x5B, Key.RWIN: 0x5C, Key.MENU: 0x5D, Key.APPS: 0x5D,
    Key.F10: 0x79, Key.CAPSLOCK: 0x14,
    Key.SPACE: 0x20, Key.ENTER: 0x0D, Key.TAB: 0x09, Key.BACKSPACE: 0x08,
    Key.DELETE: 0x2E, Key.INSERT: 0x2D, Key.HOME: 0x24, Key.END: 0x23,
    Key.PAGEUP: 0x21, Key.PAGEDOWN: 0x22, Key.ESCAPE: 0x1B,
    Key.UP: 0x26, Key.DOWN: 0x28, Key.LEFT: 0x25, Key.RIGHT: 0x27,
    Key.MINUS: 0xBD, Key.EQUALS: 0xBB, Key.LBRACKET: 0xDB, Key.RBRACKET: 0xDD,
    Key.BACKSLASH: 0xDC, Key.SEMICOLON: 0xBA, Key.APOSTROPHE: 0xDE,
    Key.COMMA: 0xBC, Key.PERIOD: 0xBE, Key.SLASH: 0xBF, Key.GRAVE: 0xC0,
}

#: Keys that must carry ``KEYEVENTF_EXTENDEDKEY``. Without it, arrow keys arrive as
#: numpad arrows and right-Ctrl as right-Alt - a genuinely confusing bug to chase from
#: game-side behaviour.
_EXTENDED_VKS: frozenset[int] = frozenset({
    0xA2, 0xA3, 0xA4, 0xA5, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x5C, 0x5D,
})

_MOUSE_BUTTON_FLAGS: dict[MouseButton, tuple[int, int]] = {
    MouseButton.LEFT: (0x0002, 0x0004),
    MouseButton.RIGHT: (0x0008, 0x0010),
    MouseButton.MIDDLE: (0x0020, 0x0040),
    MouseButton.X1: (0x0080, 0x0100),
    MouseButton.X2: (0x0080, 0x0100),
}


def is_release(primitive: Primitive) -> bool:
    """True when this primitive can only ever release something.

    Releases bypass the arm/disarm gate. That asymmetry is the whole point: a disarm that
    blocks a release strands the key.
    """
    if primitive.kind is PrimitiveType.KEY:
        return not primitive.down
    if primitive.kind is PrimitiveType.MOUSE_BUTTON:
        return not primitive.down
    return False


class SendInputPort:
    """Guarded facade over :class:`InputSafetyManager`.

    Not thread-safe by design: input is a stream, and concurrent senders reorder events.
    The executor is single-threaded per run; the estop hook runs on its own thread but only
    ever *disarms*, never injects.
    """

    name = "sendinput"

    def __init__(
        self,
        clock: ClockPort | None = None,
        *,
        dry_run: bool = False,
        run_id: str = "",
        target_hwnd: int | None = None,
        require_target_foreground: bool = True,
        allow_right_click: bool = False,
        verbose_events: bool = False,
    ) -> None:
        self._clock = clock or SystemClock()
        self._enabled = False
        self._dry_run = dry_run
        self._sent = 0
        self._blocked = 0
        self._cleanup_releases = 0
        self._last_release_report: dict = {}
        self._safety = InputSafetyManager(
            clock=clock,
            run_id=run_id,
            target_hwnd=target_hwnd,
            require_target_foreground=require_target_foreground,
            allow_right_click=allow_right_click,
            verbose_events=verbose_events,
        )

    # ------------------------------------------------------------------- properties

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def safety(self) -> InputSafetyManager:
        """The input-state authority. Exposed so the runner can audit it."""
        return self._safety

    @property
    def sent_count(self) -> int:
        return self._sent

    @property
    def cleanup_releases(self) -> int:
        """Key/button releases emitted by cleanup. Never counted as dispatched actions."""
        return self._cleanup_releases

    @property
    def blocked_count(self) -> int:
        """Primitives suppressed because the port was disarmed."""
        return self._blocked

    @property
    def last_release_report(self) -> dict:
        return dict(self._last_release_report)

    # ---------------------------------------------------------------------- control

    def set_enabled(self, value: bool, *, thorough: bool = False) -> None:
        """Arm or disarm. Disarming releases everything held.

        Arming also arms the safety manager, which is the component that actually decides
        whether to inject. The two must move together or input silently stops.

        Disarm uses the **fast** release by default. That matters: an emergency stop calls
        ``set_enabled(False)`` and then releases again, and doing the thorough 33-key sweep
        in the first of those two calls doubled the cost of an emergency stop for no
        benefit - the sweep exists for end-of-run hygiene, not for the ~16 ms it costs.
        """
        if self._enabled == value:
            return
        self._enabled = value
        if value:
            self._safety.arm()
        else:
            self.release_all(thorough=thorough)

    def arm(self) -> None:
        self.set_enabled(True)

    def disarm(self) -> None:
        self.set_enabled(False)

    def release_all(self, *, thorough: bool = True) -> dict:
        """Release every held key and button, and sweep the OS modifier state.

        ``thorough=False`` is the emergency path: held keys, both sides of every modifier,
        and the mouse buttons, without the 33-key wide sweep. That is what keeps an
        emergency stop inside its sub-100 ms budget; the full sweep is for end-of-run
        hygiene.
        """
        report = self._safety.release_all(thorough=thorough)
        self._last_release_report = report
        # Cleanup releases are counted separately. Folding them into ``sent_count`` made
        # "was anything dispatched after this point?" unanswerable, which is precisely the
        # question a disarm is supposed to guarantee.
        self._cleanup_releases += len(report.get("keys_released", []))
        self._cleanup_releases += len(report.get("buttons_released", []))
        return report

    # ---------------------------------------------------------------------- dispatch

    def send(self, primitive: Primitive, *, action_id: str = "", source: str = "") -> bool:
        """Deliver one primitive, subject to every gate. Returns whether it went out."""
        if self._dry_run:
            return False
        if not self._enabled and not is_release(primitive):
            self._blocked += 1
            return False
        delivered = self._safety.send(primitive, action_id=action_id, source=source)
        if delivered:
            self._sent += 1
        else:
            self._blocked += 1
        return delivered

    def send_batch(self, primitives: list[Primitive], *, action_id: str = "") -> int:
        sent = 0
        aid = action_id or self._safety.next_action_id()
        for primitive in primitives:
            if self.send(primitive, action_id=aid):
                sent += 1
        return sent

    def send_raw_vk(self, vk: int, *, up: bool) -> bool:
        """Press or release a raw virtual-key code, tracked like any other key.

        Needed to self-test the global-hotkey hook - the only way to press Ctrl+Alt+F12 is
        to press it. Unlike the earlier untracked escape hatch, this goes through the same
        accounting as everything else, so a release is tracked and can never be blocked.
        """
        return self._safety.send_raw_vk(vk, up=up, armed=self._enabled and not self._dry_run)

    # ----------------------------------------------------------- lifecycle & audit

    def begin_run(self):
        """Sample the input language and clear tracking before any input is sent."""
        return self._safety.begin_run()

    def post_run_check(self) -> dict:
        """Non-invasive verification. Never types into a user document or dialog."""
        return self._safety.post_run_check()

    def restore_layout(self) -> dict:
        """Restore the input language captured at ``begin_run`` if it moved."""
        return self._safety.restore_layout()

    def write_audit(self, path) -> object:
        return self._safety.write_audit(path)

    # ---------------------------------------------------------------- capabilities

    def capabilities(self) -> InputCapabilities:
        if self._dry_run:
            return InputCapabilities(notes=("dry-run: nothing reaches the OS",))
        from frameforge.actions.safety import list_layouts, virtual_size

        layouts = list_layouts()
        return InputCapabilities(
            absolute_mouse=True,
            relative_mouse=True,
            mouse_buttons=3,
            scroll=True,
            unicode=True,
            gamepad=False,
            hotkeys=True,
            notes=(
                f"virtual_desktop={virtual_size()}",
                f"input_layouts_loaded={len(layouts)}",
            ),
        )

    def position(self) -> Point:
        from frameforge.actions.safety import cursor_position

        return cursor_position()


__all__ = ["SendInputPort", "is_release"]
