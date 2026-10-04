"""Emergency stop.

Three independent paths, because any single one can be the thing that is broken:

1. **Global hotkey** on a dedicated low-level keyboard hook thread. This must work when
   the director is blocked on a slow AI call, which is why it lives in its own thread and
   never touches the executor.
2. **Sentinel file** watched by the run directory. Lets an external script or a future UI
   stop a run with no network and no console access.
3. **Programmatic** (``EstopSwitch.trigger``) plus ``SIGINT``/Ctrl-C from the CLI.

On trigger: disarm the input port *immediately*, release everything held, and mark the
run terminal. Disarm is checked inside the input adapter on every send, so an action
already queued cannot complete after the stop.

The measured requirement is that from hotkey press to zero held inputs is under 100 ms
(ROADMAP stability criterion 2). The hook path is what satisfies it; the file-watcher
path polls and is documented as slower.
"""

from __future__ import annotations

import os
import sys
import threading
import time

#: Set FRAMEFORGE_DEBUG_ESTOP=1 to trace hook exceptions. The callback swallows them by
#: design - an exception escaping a ctypes callback terminates the process - which makes a
#: silent failure otherwise impossible to diagnose.
_ESTOP_DEBUG = bool(os.environ.get("FRAMEFORGE_DEBUG_ESTOP"))

#: Left/right modifier virtual-key codes, normalised to their generic equivalents.
#:
#: A hotkey configured as ``ctrl+alt+F12`` uses VK_CONTROL (0x11) and VK_MENU (0x12), but
#: the low-level keyboard hook reports VK_LCONTROL (0xA2) and VK_LALT (0xA4). Real hardware
#: does this too - the left Ctrl key *is* 0xA2. Matching only the generic code therefore
#: means the emergency stop silently never fires, which is the worst possible failure for
#: the one control that must always work.
VK_NORMALISE: dict[int, int] = {
    0xA0: 0x10, 0xA1: 0x10,   # LSHIFT / RSHIFT -> VK_SHIFT
    0xA2: 0x11, 0xA3: 0x11,   # LCTRL  / RCTRL  -> VK_CONTROL
    0xA4: 0x12, 0xA5: 0x12,   # LALT   / RALT   -> VK_MENU
    0x5C: 0x5B,               # RWIN -> LWIN
}
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from frameforge.kernel.clock import ClockPort, SystemClock


class EstopSource(StrEnum):
    HOTKEY = "hotkey"
    SENTINEL_FILE = "sentinel_file"
    PROGRAMMATIC = "programmatic"
    SIGNAL = "signal"


@dataclass(slots=True)
class EstopState:
    triggered: bool = False
    source: EstopSource | None = None
    detail: str = ""
    mono_ms: float | None = None
    latency_ms: float | None = None
    input_released: bool = False


class EstopSwitch:
    """Latching emergency stop. Once triggered it stays triggered.

    Latching is deliberate. A stop that could be un-triggered is a stop that a bug could
    un-trigger; a human restarts the run instead.
    """

    def __init__(
        self,
        input_port=None,
        *,
        clock: ClockPort | None = None,
        hotkey: str = "ctrl+alt+F12",
        sentinel_path: Path | None = None,
        on_trigger: Callable[[EstopState], None] | None = None,
    ) -> None:
        self._input = input_port
        self._clock = clock or SystemClock()
        self._hotkey = hotkey
        self._sentinel = Path(sentinel_path) if sentinel_path else None
        self._on_trigger = on_trigger
        self._state = EstopState()
        self._lock = threading.Lock()
        self._hook_handle: threading.Thread | None = None
        self._hook_active = False
        self._hook_proc = None
        self._hook_state: dict = {}
        self._hotkey_combo: set[int] = set()
        self._parse_hotkey(hotkey)

    # -------------------------------------------------------------------- state

    @property
    def state(self) -> EstopState:
        return self._state

    @property
    def triggered(self) -> bool:
        return self._state.triggered

    @property
    def hotkey(self) -> str:
        return self._hotkey

    def trigger(
        self,
        source: EstopSource = EstopSource.PROGRAMMATIC,
        detail: str = "",
    ) -> EstopState:
        """Fire the stop. Idempotent; the first source wins."""
        with self._lock:
            if self._state.triggered:
                return self._state
            t0 = self._clock.monotonic_ms()
            self._state.triggered = True
            self._state.source = source
            self._state.detail = detail or source.value
            self._state.mono_ms = t0
            if self._input is not None:
                # Injected input from inside a low-level keyboard hook callback is
                # re-entrant: the key-ups we send come back through the very hook we are
                # inside, and Windows may drop them. A modifier left down after an estop is
                # indistinguishable from one left down after a normal run - every later
                # keystroke reads as a chord.
                #
                # So the hotkey path only latches here; the actual release runs on a
                # dedicated thread. The stop is still effectively instant because the latch
                # gates all further input.
                if source is EstopSource.HOTKEY:
                    self._state.input_released = False
                    threading.Thread(
                        target=self._release_off_hook, name="ff-estop-release", daemon=True
                    ).start()
                else:
                    self._release_now()
            self._state.latency_ms = self._clock.monotonic_ms() - t0
            if self._on_trigger:
                try:
                    self._on_trigger(self._state)
                except Exception:
                    pass
        return self._state

    def _release_now(self) -> None:
        """Release held input, then disarm. Used on every path except the hook thread.

        Uses the **fast** release. An emergency stop must free the user's keyboard
        immediately; the thorough 33-key sweep is valuable but is not a stop - measured at
        ~27 ms against ~12 ms here, and several hundred milliseconds when the machine is
        under test load. End-of-run cleanup does the thorough pass.
        """
        if self._input is None:
            return
        try:
            # Release while still enabled so the sends are not dropped.
            self._input.release_all(thorough=False)
            self._input.set_enabled(False, thorough=False)
            self._state.input_released = True
        except Exception:
            self._state.input_released = False

    def _release_off_hook(self) -> None:
        """Deferred release for the hotkey path, run off the hook callback thread."""
        time.sleep(0.02)
        with self._lock:
            self._release_now()

    def reset(self) -> None:
        """Re-arm. Only for a new run; never during one."""
        with self._lock:
            self._state = EstopState()

    # ------------------------------------------------------------------ hotkey

    def _parse_hotkey(self, combo: str) -> None:
        import ctypes

        VK = {
            "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
            "win": 0x5B, "f12": 0x7B, "f1": 0x70, "f2": 0x71, "f3": 0x72,
            "escape": 0x1B, "space": 0x20,
        }
        for i in range(1, 25):
            VK[f"f{i}"] = 0x6F + i
        for part in combo.lower().split("+"):
            part = part.strip()
            if part in VK:
                # Store the generic code; the hook normalises to the same form.
                self._hotkey_combo.add(VK[part])
        _ = ctypes  # keep the import meaningful for readers

    def install_hotkey(self) -> bool:
        """Install the low-level keyboard hook. Returns success.

        Runs its own thread so the hook keeps servicing messages even while the director
        is blocked on a remote AI call.
        """
        if not self._hotkey_combo or self._hook_handle is not None:
            return False

        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
        WM_QUIT = 0x0012

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_longlong
        user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = ctypes.c_longlong

        target = self._hotkey_combo
        switch = self
        state = {"down": set(), "pressed": set(), "handle": None, "thread_id": 0,
                 "ready": False, "error": ""}

        def proc(code: int, wparam: int, lparam: int) -> int:
            """Low-level keyboard hook callback.

            Two rules keep this from taking the process down, both learned the hard way:

            * ``CallNextHookEx`` MUST be called for every message, or the whole input
              chain stalls for the user.
            * Nothing in here may raise. An exception escaping a ctypes callback
              propagates into the C stack and terminates the process - and this is the
              emergency stop, so a crash here is the worst possible outcome. Hence the
              blanket try/except.
            """
            try:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = VK_NORMALISE.get(int(info.vkCode), int(info.vkCode))
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if vk not in state["pressed"]:
                            state["pressed"].add(vk)
                            state["down"].add(vk)
                            # Fire only on the full combo, and only once: a partial match
                            # must never stop a run, and auto-repeat must not re-trigger.
                            if target and target.issubset(state["down"]):
                                switch.trigger(EstopSource.HOTKEY, f"global hotkey {switch.hotkey}")
                                state["down"].clear()
                    else:
                        state["down"].discard(vk)
                        state["pressed"].discard(vk)
            except Exception:
                # Swallow. A stop hook that raises is a stop hook that kills the process.
                #
                # The cost of swallowing is that a NameError here is invisible - which is
                # exactly what happened during development: the callback was entered on
                # every keypress and silently did nothing because a constant was missing.
                # Set FRAMEFORGE_DEBUG_ESTOP=1 to surface hook exceptions.
                if _ESTOP_DEBUG:
                    import traceback

                    print("[estop-hook] exception in callback:", file=sys.stderr)
                    traceback.print_exc()
            return user32.CallNextHookEx(None, code, wparam, lparam)

        # CRITICAL: the HOOKPROC object MUST outlive this function. Passing a temporary
        # lets CPython collect it, and the installed hook then points at freed memory -
        # which crashes the process the first time a key is pressed. That was observed as
        # a hard crash in the hardware test, not theorised.
        #
        # SECOND CRITICAL POINT: a low-level keyboard hook is invoked on the thread that
        # installed it, and that thread must pump messages. Installing from the caller's
        # thread and pumping on a different one produces a hook that is silently never
        # called - which is exactly what the hardware test observed.
        self._hook_proc = HOOKPROC(proc)
        # Set BEFORE the thread starts. The loop condition below reads this flag, and
        # using `_hook_handle` for it raced: the thread could evaluate it before the main
        # thread assigned the handle, exit at once, and leave a hook that appeared to
        # install but never received a single event. Found by the hardware test.
        self._hook_active = True

        def run() -> None:
            import ctypes as _ctypes

            state["thread_id"] = _ctypes.windll.kernel32.GetCurrentThreadId()
            handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc, None, 0)
            if not handle:
                state["error"] = "SetWindowsHookExW failed"
                state["ready"] = True
                return
            state["handle"] = handle
            # Force this thread's message queue into existence *before* returning.
            #
            # A low-level hook's messages are delivered to the installing thread's queue,
            # and that queue is not created until a message API is called. If input arrives
            # in the window between SetWindowsHookExW and the first GetMessageW, the
            # messages have nowhere to go and the hook appears installed but dead. Peek
            # creates the queue without blocking. Observed as "hook installed, zero events"
            # in the hardware test.
            msg0 = wintypes.MSG()
            user32.PeekMessageW(_ctypes.byref(msg0), None, 0, 0, 0)
            state["ready"] = True
            msg = wintypes.MSG()
            while self._hook_active:
                result = user32.GetMessageW(_ctypes.byref(msg), None, 0, 0)
                if result in (0, -1):
                    break
                user32.TranslateMessage(_ctypes.byref(msg))
                user32.DispatchMessageW(_ctypes.byref(msg))

        thread = threading.Thread(target=run, name="ff-estop-hook", daemon=True)
        thread.start()
        # Wait for the hook to actually be installed before returning. A caller that
        # presses the hotkey immediately after install_hotkey() must not race the install.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not state.get("ready"):
            time.sleep(0.01)
        if not state.get("ready"):
            self._hook_active = False
            self._hook_handle = None
            self._hook_proc = None
            return False
        if not state.get("handle"):
            self._hook_active = False
            self._startup_error = state.get("error", "hook install failed")
            self._hook_handle = None
            self._hook_proc = None
            return False

        self._hook_handle = thread
        self._hook_state = state
        return True

    def uninstall_hotkey(self) -> None:
        """Remove the hook.

        Posts WM_QUIT so the message loop exits, unhooks, and drops the HOOKPROC reference
        last. Dropping it earlier would leave a live hook pointing at freed memory.
        """
        thread = self._hook_handle
        self._hook_handle = None
        self._hook_active = False
        state = getattr(self, "_hook_state", None)
        if state and state.get("handle"):
            try:
                import ctypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(state.get("thread_id", 0), 0x0012, 0, 0)
                if thread is not None:
                    thread.join(timeout=2.0)
                user32.UnhookWindowsHookEx(state["handle"])
            except Exception:
                pass
        state["handle"] = None if state else None
        self._hook_proc = None

    # -------------------------------------------------------------- sentinel file

    def write_sentinel(self) -> None:
        """Write the stop sentinel. ``touch runs/<id>/ABORT`` is enough to stop a run."""
        if self._sentinel is not None:
            self._sentinel.parent.mkdir(parents=True, exist_ok=True)
            self._sentinel.write_text("abort", encoding="utf-8")

    def poll_sentinel(self) -> bool:
        """Check the sentinel. Call from the director loop; cheap by design."""
        if self._sentinel is not None and self._sentinel.exists():
            self.trigger(EstopSource.SENTINEL_FILE, str(self._sentinel))
            return True
        return False

    def sentinel_path_str(self) -> str | None:
        return str(self._sentinel) if self._sentinel else None


__all__ = ["EstopSource", "EstopState", "EstopSwitch"]
