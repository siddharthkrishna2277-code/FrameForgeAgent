"""The diagnostic panel must be incapable of authorising or dispatching input.

Asserted structurally (AST + import graph), not by convention: a panel that merely chooses
not to call the runner is one refactor away from being able to.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PANEL = (Path(__file__).resolve().parents[2]
         / "src" / "frameforge" / "ui" / "status_panel.py")
SRC = Path(__file__).resolve().parents[2] / "src" / "frameforge"


def _tree() -> ast.Module:
    return ast.parse(PANEL.read_text(encoding="utf-8"))


def test_panel_does_not_import_the_execution_path():
    """No import of the runner, executor, arm token, controller or input backend."""
    tree = _tree()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{a.name}" for a in node.names)
    forbidden = ("runner", "executor", "arm", "controller", "safety",
                 "sendinput", "target_guard", "input")
    for name in imported:
        tail = name.rsplit(".", 1)[-1].lower()
        assert not any(f in tail for f in forbidden), (
            f"status_panel imports {name}; a read-only view must not reach the execution path")


def test_panel_never_calls_a_mutating_method():
    """Calling arm/confirm/execute would make this an input surface.

    Checked against identifiers and call targets, not raw substrings: the gate evidence
    strings legitimately contain the word "dispatch", and a substring scan would both miss
    an aliased call and fail on documentation.
    """
    tree = _tree()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(getattr(fn, "attr", None) or getattr(fn, "id", None) or "")
        elif isinstance(node, ast.Attribute):
            called.add(node.attr)
    mutators = {"arm_for_live_input", "confirm_live_input", "execute", "dispatch",
                "begin_countdown", "confirm_active", "SendInput", "keybd_event",
                "mouse_event", "SetCursorPos", "press", "click", "release", "inject"}
    hit = {c for c in called if c in mutators}
    assert not hit, f"status_panel references mutating operations: {sorted(hit)}"


def test_panel_defines_no_buttons_or_callbacks():
    """A Button or a command= callback would be a control surface."""
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name in ("Button", "Checkbutton", "Radiobutton", "Entry", "Scale", "Spinbox"):
                raise AssertionError(f"status_panel creates an interactive widget: {name}")


def test_panel_only_reads_runner_state():
    """snapshot_from_runner may call getters, never mutators."""
    tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "snapshot_from_runner")
    called = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(getattr(f, "attr", None) or getattr(f, "id", None))
    mutators = {"arm", "arm_for_live_input", "confirm", "confirm_live_input",
                "execute", "dispatch", "run", "start", "stop", "pause", "resume",
                "press", "click", "release"}
    assert not (called & mutators), f"snapshot_from_runner mutates: {called & mutators}"


def test_gate_register_states_scope_for_every_row():
    """'verified' without a scope is the ambiguity this test exists to prevent."""
    from frameforge.ui.status_panel import (
        GATE_REGISTER, SCOPE_LIVE_DESKTOP, SCOPE_LIVE_ISOLATED, SCOPE_MOCK,
        SCOPE_NOT_IMPLEMENTED, SCOPE_NOT_VERIFIED, SCOPE_PARTIAL,
    )

    allowed = {SCOPE_MOCK, SCOPE_LIVE_ISOLATED, SCOPE_LIVE_DESKTOP,
               SCOPE_NOT_VERIFIED, SCOPE_PARTIAL, SCOPE_NOT_IMPLEMENTED}
    for row in GATE_REGISTER:
        assert row.scope in allowed, f"{row.name} has unrecognised scope {row.scope!r}"
        assert row.scope != "COMPLETE_AND_VERIFIED", (
            f"{row.name} claims bare verification; scope is mandatory")
        assert row.evidence.strip(), f"{row.name} states no evidence"


def test_no_gate_claims_live_desktop_verification():
    """Nothing in this build has been validated with live input, so nothing may say so."""
    from frameforge.ui.status_panel import GATE_REGISTER, SCOPE_LIVE_DESKTOP, SCOPE_LIVE_ISOLATED

    for row in GATE_REGISTER:
        assert row.scope not in (SCOPE_LIVE_DESKTOP, SCOPE_LIVE_ISOLATED), (
            f"{row.name} claims live verification that has not happened")


def test_panel_json_reports_itself_read_only():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "frameforge.cli.main", "panel", "--json"],
        cwd=root, capture_output=True, text=True, timeout=180,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
    )
    assert r.returncode == 0, r.stderr
    import json

    payload = json.loads(r.stdout)
    assert payload["read_only"] is True
    assert payload["has_arm_control"] is False
    assert payload["input_locked"] is True


def test_panel_subcommand_has_no_arm_option():
    """The CLI surface itself must not offer an arming control."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, "-m", "frameforge.cli.main", "panel", "--help"],
        cwd=root, capture_output=True, text=True, timeout=180,
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout.lower()
    for forbidden in ("--arm", "--attest", "--confirm", "--target", "--execute"):
        assert forbidden not in out, f"panel exposes {forbidden}"
