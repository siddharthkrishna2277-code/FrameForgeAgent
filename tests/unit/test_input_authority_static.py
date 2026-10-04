"""Static guard: one exclusive input authority.

This is the CI check that makes rule G-ABS-11 enforceable rather than aspirational.
Developer discipline is not a control; a test that fails the build is.

Policy, stated once:

* Exactly one module may call ``SendInput`` or its equivalents: the approved backend,
  ``frameforge/actions/safety.py``.
* No script, test, demo, diagnostic or helper may call them at all.
* Nothing may import ``pyautogui``, ``pynput``, ``keyboard``, ``mouse`` or AutoHotkey.
* No test may hard-code an absolute screen coordinate for injection.

The allowlist is named here rather than inferred, so widening it is a visible, reviewable
edit to this file rather than a silent exception.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The one module permitted to inject, as a path relative to the scan roots.
APPROVED_INJECTOR = "src/frameforge/actions/safety.py"

#: APIs that can affect the machine's input state.
FORBIDDEN_APIS = frozenset({
    "SendInput", "keybd_event", "mouse_event", "SetCursorPos",
})

#: Third-party injection libraries, banned outright rather than merely unused.
FORBIDDEN_IMPORTS = frozenset({
    "pyautogui", "pynput", "keyboard", "mouse", "ahk", "autohotkey",
    "pywinauto", "uiautomation",
})

#: Scanned trees. ``.venv`` and build output are excluded.
SCAN_ROOTS = ("src", "tests", "scripts", "fixtures")


def _python_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


class TestExclusiveInputAuthority:
    def test_only_the_approved_module_calls_sendinput(self):
        offenders: list[str] = []
        for path in _python_files():
            rel = path.relative_to(REPO).as_posix()
            if rel == APPROVED_INJECTOR:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                offenders.append(f"{rel}: unparseable ({exc})")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_APIS:
                    offenders.append(f"{rel}:{node.lineno} {node.attr}")
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_APIS:
                    offenders.append(f"{rel}:{node.lineno} {node.id}")
        assert not offenders, (
            "direct input API outside the approved backend "
            f"{APPROVED_INJECTOR}: {offenders}"
        )

    def test_approved_backend_does_call_sendinput(self):
        """Sanity check on the allowlist: if it stopped injecting, the policy is vacuous."""
        path = REPO / APPROVED_INJECTOR
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "SendInput"
        ]
        assert calls, "the approved backend no longer calls SendInput; the guard is now vacuous"

    def test_no_injection_library_is_imported_anywhere(self):
        offenders: list[str] = []
        for path in _python_files():
            rel = path.relative_to(REPO).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0].lower()
                    if top in FORBIDDEN_IMPORTS:
                        offenders.append(f"{rel}:{node.lineno} {name}")
        assert not offenders, f"injection library imported: {offenders}"

    def test_no_fixed_absolute_screen_point_for_injection(self):
        """A literal pixel is unauthorised by definition: it belongs to whatever is there."""
        pattern = re.compile(r"MOUSE_MOVE_ABS\s*,\s*x\s*=\s*\d+\s*,\s*y\s*=\s*\d+")
        offenders: list[str] = []
        for root in ("tests", "scripts", "fixtures"):
            base = REPO / root
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                for m in pattern.finditer(path.read_text(encoding="utf-8")):
                    offenders.append(
                        f"{path.relative_to(REPO).as_posix()}: {m.group(0)}")
        assert not offenders, f"hard-coded absolute screen point: {offenders}"

    def test_bypass_scripts_are_gone(self):
        """The two scripts that injected raw SendInput must not come back."""
        for name in ("probe_estop_hook.py", "probe_input_timing.py"):
            assert not (REPO / "scripts" / name).exists(), (
                f"scripts/{name} bypasses the Input Safety Controller and must not exist"
            )

    def test_no_script_calls_a_process_killing_input_api(self):
        """A script that can inject is a script that can corrupt the operator's input."""
        offenders: list[str] = []
        for path in (REPO / "scripts").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "SendInputPort(" in text and "dry_run=True" not in text:
                offenders.append(path.name)
        assert not offenders, (
            f"scripts constructing a live input port: {offenders}. Diagnostics must be "
            "read-only or mock-backed."
        )
