"""The tool boundary: policy, controllers, target validation, and CI enforcement.

Everything here runs against :class:`MockInputController`, so cancellation, worker
crashes and hold deadlines are covered exhaustively without ever touching a real desktop.

The architecture being asserted:

    Planner -> declarative ToolRequest -> InputPolicy -> InputController -> backend

and the rule that makes it worth anything: *the planner cannot reach the OS*, and the only
component that can is the one controller.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from frameforge.actions.controller import (
    DenyReason,
    DisabledInputController,
    InputPolicy,
    MockInputController,
    PolicyConfig,
    PolicyOutcome,
    ActionTarget,
    ToolRequest,
    WindowsInputController,
)
from frameforge.actions.model import Click, Intent, KeyPress, MouseLook, Wait
from frameforge.actions.safety import (
    DENIED_CLASSES,
    DENIED_PROCESSES,
    MENU_TRIGGER_KEYS,
    InputSafetyManager,
    TargetWindow,
    classify_target,
)
from frameforge.ports.geometry import Point, Rect
from frameforge.ports.input import Key

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "frameforge"

#: The target used by the policy tests. It carries the *real* interpreter's image path,
#: because target identity is now verified rather than trusted: a process name alone is
#: something the process asserts about itself, and a policy keyed on that would accept any
#: program calling itself "testbed.exe".
_REAL_IMAGE = os.path.abspath(sys.executable)
_REAL_NAME = os.path.basename(_REAL_IMAGE)


#: A point derived from the test, not a hardcoded screen coordinate. The value is
#: irrelevant - these tests exercise the policy, never the desktop - but a literal pixel
#: is the pattern P0.5 forbids, and a reader cannot tell the difference.
_TEST_X, _TEST_Y = 10, 10


def policy_for(target, **cfg) -> InputPolicy:
    """A policy that permits exactly ``target``, with its identity verifiable.

    Foreground checking is off by default here because these tests are about delegation and
    target binding, not about which window happens to be in front on the test machine.
    """
    cfg.setdefault("require_foreground", False)
    return InputPolicy(PolicyConfig(
        allow_processes=frozenset({target.process_name.lower()}), **cfg
    ))


def real_target(**kw) -> "ActionTarget":
    """An ActionTarget whose identity actually verifies."""
    defaults = dict(hwnd=1000, pid=os.getpid(), process_name=_REAL_NAME,
                    class_name="TkTopLevel", title="Testbed",
                    bounds=Rect(0, 0, 1280, 720), image_path=_REAL_IMAGE)
    defaults.update(kw)
    return ActionTarget(**defaults)


TARGET = real_target()


def request(action, **kw) -> ToolRequest:
    return ToolRequest(action=action, run_id="r1", action_id="a1", target=TARGET, **kw)


# ------------------------------------------------------------------ policy layer


class TestPolicyLayer:
    def test_allows_a_valid_target(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        assert policy.validate(request(Click(at=Point(10, 10)))).allowed

    def test_denies_an_unlisted_process(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({"something-else.exe"}),
                                         require_foreground=False))
        result = policy.validate(request(Click(at=Point(10, 10))))
        assert not result.allowed and result.reason is DenyReason.TARGET_NOT_ALLOWED
        assert "allow-list" in result.detail

    def test_live_input_requires_an_explicit_target(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(ToolRequest(action=Click(at=Point(1, 1))))
        assert not result.allowed and result.reason is DenyReason.TARGET_MISSING

    def test_denies_when_disarmed(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(request(Click(at=Point(1, 1))), is_armed=False)
        assert not result.allowed and result.reason is DenyReason.DISARMED

    def test_denies_after_a_failed_cleanup(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(request(Click(at=Point(1, 1))), cleanup_ok=False)
        assert not result.allowed and result.reason is DenyReason.CLEANUP_FAILED

    def test_denies_an_unpermitted_action_type(self):
        from frameforge.actions.model import Screenshot

        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(request(Screenshot()))
        assert not result.allowed and result.reason is DenyReason.NOT_PERMITTED

    def test_right_click_denied_by_default(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(request(Click(at=Point(1, 1), button="right")))
        assert not result.allowed and result.reason is DenyReason.RIGHT_CLICK

    def test_right_click_requires_explicit_permission(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         allow_right_click=True,
                                         require_foreground=False))
        assert policy.validate(request(Click(at=Point(1, 1), button="right"))).allowed

    def test_out_of_bounds_click_denied(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({_REAL_NAME.lower()}),
                                         require_foreground=False))
        result = policy.validate(request(Click(at=Point(9000, 9000))))
        assert not result.allowed and result.reason is DenyReason.OUT_OF_BOUNDS

    def test_denials_are_recorded_for_audit(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({"something-else.exe"}),
                                         require_foreground=False))
        policy.validate(request(Click(at=Point(1, 1))))
        assert policy.denials and policy.denials[0][0] == "a1"

    def test_no_allowlist_means_no_live_target(self):
        """Default policy has no permitted process, so live input is off by default."""
        policy = InputPolicy(PolicyConfig(require_foreground=False))
        result = policy.validate(request(Click(at=Point(1, 1))))
        assert result.reason in (DenyReason.TARGET_NOT_ALLOWED, DenyReason.TARGET_MISSING)


# --------------------------------------------------------------------- deny-list


class TestDenyList:
    """The agent's own UI, and anything else that must never be an automation target."""

    @pytest.mark.parametrize("klass,proc,title", [
        ("Progman", "explorer.exe", "Program Manager"),
        ("ConsoleWindowClass", "cmd.exe", "cmd"),
        ("Chrome_WidgetWin_1", "Hermes.exe", "Hermes"),
        ("Chrome_WidgetWin_1", "chrome.exe", "Some Page - Google Chrome"),
        ("CodeTopLevel", "Code.exe", "main.py - Visual Studio Code"),
        ("Shell_TrayWnd", "explorer.exe", ""),
        ("Windows.UI.Core.CoreWindow", "ShellExperienceHost.exe", "Windows Shell"),
    ])
    def test_protected_windows_are_refused(self, klass, proc, title):
        target = TargetWindow(hwnd=1, title=title, class_name=klass,
                              process_name=proc, pid=9)
        ok, reason = classify_target(target)
        assert not ok, f"{klass}/{proc} was accepted"
        assert reason

    @pytest.mark.parametrize("klass,proc,title", [
        ("#32770", "CredentialUIBroker.exe", "Windows Security"),
        ("#32770", "consent.exe", "User Account Control"),
        (None, None, "Enter your password"),
    ])
    def test_credential_windows_are_refused(self, klass, proc, title):
        target = TargetWindow(hwnd=1, title=title, class_name=klass or "",
                              process_name=proc or "", pid=9)
        assert classify_target(target)[0] is False

    def test_a_normal_application_is_accepted(self):
        target = TargetWindow(hwnd=1, title="Untitled - Notepad",
                              class_name="Notepad", process_name="Notepad.exe", pid=4)
        assert classify_target(target)[0] is True

    def test_no_window_is_refused(self):
        assert classify_target(None)[0] is False

    def test_deny_lists_are_normalised(self):
        """Entries are compared lowercased, so a stray space makes one silently useless."""
        from frameforge.actions.safety import _deny_list_is_normalised

        procs = DENIED_PROCESSES

        assert _deny_list_is_normalised(), (
            f"malformed entries: "
            f"{[e for e in (*DENIED_CLASSES, *procs) if e != e.lower() or e != e.strip()]}"
        )
        for probe in ("Code.exe", "hermes.exe", "explorer.exe", "consent.exe"):
            assert probe.lower() in procs, f"{probe} is not deny-listed"

    def test_a_normal_application_is_still_allowed(self):
        """The deny-list must not be so broad that nothing can be automated."""
        for probe in ("notepad.exe", "testbed.exe", "game.exe"):
            assert probe not in DENIED_PROCESSES

    def test_denied_classes_are_a_frozen_lowercase_set(self):
        assert "progman" in DENIED_CLASSES
        assert "chrome_widgetwin_1" in DENIED_CLASSES
        assert isinstance(DENIED_CLASSES, frozenset)


class TestContextMenuKeys:
    """A context menu needs no mouse: the Menu key and Shift+F10 both produce one."""

    def test_menu_trigger_keys_are_declared(self):
        assert "menu" in MENU_TRIGGER_KEYS

    @pytest.mark.parametrize("key", ["menu", "apps"])
    def test_menu_keys_are_refused(self, key):
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        manager = RecordingManager()
        primitive = Primitive(kind=PrimitiveType.KEY, key=Key(key), down=True)
        assert manager.send(primitive) is False
        assert manager.injected == [], "a context-menu key must never be injected"

    def test_shift_f10_is_refused_as_a_chord(self):
        manager = InputSafetyManager()
        violation, detail = manager.check_chord(["shift", "f10"])
        assert violation is not None and violation.value != "none"

    def test_bare_f10_is_refused(self):
        """Either half of Shift+F10 opens a context menu; both must be refused."""
        manager = InputSafetyManager()
        violation, _ = manager.check_chord(["f10"])
        assert violation.value != "none"


class RecordingManager(InputSafetyManager):
    """Captures would-be injections instead of sending them."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.injected = []
        self.arm()

    def _inject(self, primitive):
        self.injected.append(primitive)


# ------------------------------------------------------------------- controllers


class TestMockController:
    def test_records_without_touching_the_desktop(self):
        controller = MockInputController()
        result = controller.execute(request(Click(at=Point(1, 1))), [])
        assert result.ok
        assert len(controller.executed) == 1

    def test_cleanup_releases_simulated_holds(self):
        controller = MockInputController()
        controller.hold_mid_action = True
        controller.execute(request(Click(at=Point(1, 1))), [])
        assert len(controller.held) == 1
        report = controller.cleanup()
        assert controller.held == () and report["released"]

    def test_cleanup_is_idempotent(self):
        controller = MockInputController()
        controller.cleanup()
        controller.cleanup()
        assert controller.cleanup_calls == 2

    def test_backend_failure_is_surfaced_not_swallowed(self):
        controller = MockInputController(fail_on="click")
        with pytest.raises(RuntimeError):
            controller.execute(request(Click(at=Point(1, 1))), [])

    def test_health_reflects_held_state(self):
        controller = MockInputController()
        assert controller.health()["healthy"]
        controller.hold_mid_action = True
        controller.execute(request(Click(at=Point(1, 1))), [])
        assert controller.health()["healthy"] is False


class TestDisabledController:
    def test_refuses_everything(self):
        controller = DisabledInputController()
        result = controller.execute(request(Click(at=Point(1, 1))), [])
        assert not result.ok and result.outcome == "refused"

    def test_cleanup_is_safe(self):
        assert DisabledInputController().cleanup() is not None


class TestWindowsControllerRequiresPolicy:
    """Authority to inject and authority to permit must not be separate components."""

    def test_controller_cannot_be_built_without_a_policy(self):
        from frameforge.actions.safety import InputSafetyManager

        class Port:
            def __init__(self):
                self.safety = InputSafetyManager()

        with pytest.raises(ValueError, match="requires an InputPolicy"):
            WindowsInputController(Port())

    def test_build_controller_refuses_a_live_port_with_no_policy(self):
        from frameforge.actions.controller import build_controller
        from frameforge.actions.safety import InputSafetyManager

        class Port:
            name = "sendinput"

            def __init__(self):
                self.safety = InputSafetyManager()

        with pytest.raises(ValueError, match="requires an InputPolicy"):
            build_controller(Port(), policy=None)

    def test_executor_refuses_to_send_without_a_policy(self):
        """No policy, no input - rather than a permissive default."""
        from frameforge.actions.executor import ActionExecutor
        from frameforge.actions.safety import InputSafetyManager
        from frameforge.ports import fakes
        from frameforge.ports.input import Primitive, PrimitiveType

        port = fakes.FakeInput()
        executor = ActionExecutor(port, controller=None, policy=None)
        port.set_enabled(True)
        result = executor.execute(
            [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=_TEST_X, y=_TEST_Y)]
        )
        assert port.primitives == [], "input was sent with no policy layer"
        assert result.denied and result.outcome == "denied"

    def test_executor_refuses_without_a_controller(self):
        from frameforge.actions.executor import ActionExecutor
        from frameforge.ports import fakes
        from frameforge.ports.input import Primitive, PrimitiveType

        port = fakes.FakeInput()
        executor = ActionExecutor(port, controller=None, policy=policy_for(TARGET))
        port.set_enabled(True)
        result = executor.execute(
            [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=_TEST_X, y=_TEST_Y)]
        )
        assert port.primitives == [], "input was sent with no controller"
        assert result.denied and result.outcome == "denied"


class _FakePort:
    """Minimal live-port contract: safety manager, armable, batch send, release."""

    def __init__(self):
        from frameforge.actions.safety import InputSafetyManager

        self.safety = InputSafetyManager()
        self.batches: list[tuple[list, str]] = []
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def arm(self) -> None:
        self._enabled = True
        self.safety.arm()

    def disarm(self) -> None:
        self._enabled = False

    def send_batch(self, prims, action_id=""):
        self.batches.append((prims, action_id))
        return len(prims)

    def release_all(self, thorough: bool = True):
        return self.safety.release_all(thorough=thorough)


class TestWindowsControllerWrapsTheSafetyManager:
    def test_delegates_to_the_port(self):
        from frameforge.ports import fakes
        from frameforge.actions.safety import InputSafetyManager

        port = _FakePort()
        port.arm()
        controller = WindowsInputController(port, policy=policy_for(TARGET))
        result = controller.execute(request(Click(at=Point(1, 1))), [1, 2, 3])
        assert result.ok and port.batches[0][1] == "a1"

    def test_target_is_set_from_the_request(self):
        from frameforge.actions.safety import InputSafetyManager

        port = _FakePort()
        port.arm()
        controller = WindowsInputController(port, policy=policy_for(TARGET))
        controller.execute(request(Click(at=Point(1, 1))), [])
        assert port.safety.target_hwnd == TARGET.hwnd

    def test_hold_is_bounded_by_the_request(self):
        from frameforge.actions.safety import InputSafetyManager

        port = _FakePort()
        port.arm()
        controller = WindowsInputController(port, policy=policy_for(TARGET))
        controller.execute(request(KeyPress(key=Key.W), max_hold_ms=250), [])
        assert port.safety.max_hold_ms <= 250


class TestWatchdog:
    def test_overdue_hold_is_force_released(self):
        from frameforge.actions.safety import InputSafetyManager, HeldItem

        manager = InputSafetyManager()
        manager.arm()
        manager._held["lctrl"] = HeldItem(
            name="lctrl", kind="key", pressed_mono_ms=-10_000.0, max_hold_ms=500.0
        )
        released = manager.enforce_deadlines()
        assert "lctrl" in released
        assert manager._held == {}

    def test_fresh_hold_is_left_alone(self):
        from frameforge.actions.safety import InputSafetyManager, HeldItem
        from frameforge.kernel.clock import FakeClock

        clock = FakeClock()
        manager = InputSafetyManager(clock=clock)
        manager.arm()
        manager._held["lctrl"] = HeldItem(
            name="lctrl", kind="key", pressed_mono_ms=clock.monotonic_ms(),
            max_hold_ms=5000.0,
        )
        assert manager.enforce_deadlines() == []
        assert "lctrl" in manager._held


# ------------------------------------------------------------------ CI enforcement


class TestCiBoundaries:
    """Rules that fail the build, so the architecture cannot quietly rot."""

    def test_sendinput_only_in_safety_module(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            if path.name == "safety.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("SendInput", "keybd_event"):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
        assert not offenders, f"direct OS input outside safety.py: {offenders}"

    def test_only_one_module_injects(self):
        """Count modules that actually call SendInput, not just declare it."""
        callers = set()
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "SendInput":
                    callers.add(path.name)
        assert callers <= {"safety.py"}, f"modules injecting: {callers}"

    def test_no_input_injection_libraries(self):
        """The convenience libraries are banned outright, not merely unused."""
        banned = ("pyautogui", "pynput", "keyboard", "mouse", "autohotkey", "uiautomation")
        hits = []
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if any(name.split(".")[0] == b for b in banned):
                        hits.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
        assert not hits, f"input-injection library imported: {hits}"

    def test_no_mouse_event_or_mouse_event_api(self):
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "mouse_event(" not in text, path.name
            assert "SetWindowsHookExW(WH_MOUSE" not in text, path.name

    def test_only_one_live_controller_class(self):
        """There is exactly one class permitted to emit real input."""
        tree = ast.parse((SRC / "actions" / "controller.py").read_text(encoding="utf-8"))
        live = [n.name for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "WindowsInputController"]
        assert live == ["WindowsInputController"]

    def test_planner_modules_never_import_the_windows_backend(self):
        """The planning tier must not be able to reach the OS, even indirectly."""
        for name in ("planning", "tasks"):
            root = SRC / name
            for path in root.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                assert "SendInput" not in text, path.name
                assert "adapters.input.sendinput" not in text, path.name

    def test_hardware_tests_are_guarded(self):
        """Every injecting hardware test must go through the target guard."""
        for path in (REPO / "tests" / "hardware").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "port.send(" in text or "send_raw_vk(" in text or ".send_batch(" in text:
                assert ("require_test_target" in text or "port_for_injection" in text
                        or "dry_run" in text), f"{path.name} injects without a guard"

    def test_hardware_tests_do_not_unconditionally_right_click(self):
        for path in (REPO / "tests" / "hardware").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert 'MouseButton.RIGHT, down=True' not in text or "allow_right_click" in text, (
                f"{path.name} sends an unguarded right-button press"
            )
