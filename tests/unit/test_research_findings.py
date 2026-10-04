"""Tests for the four findings adopted from the Codex research (round 2).

Each group maps to one gap the research identified in Frame Forge. They are grouped here
rather than scattered so the provenance stays traceable: docs/RESEARCH_CODEX_2.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from frameforge.actions.controller import (
    ActionTarget,
    DenyReason,
    InputPolicy,
    PolicyConfig,
    ToolRequest,
)
from frameforge.actions.model import KeyPress
from frameforge.ports.input import Key


# ------------------------------------------------------- gap 1: target integrity


class TestTargetIntegrity:
    """Evidence was being collected and never checked.

    Every dispatch record carries the foreground window at the moment of the event. Nothing
    looked at it, so Frame Forge could say "nothing was left held" but not "input only went
    where it was allowed to".
    """

    def test_clean_when_every_event_hit_the_target(self):
        from frameforge.actions.safety import InputSafetyManager

        m = InputSafetyManager(target_hwnd=1000)
        m.arm()
        # Two events, both with the target foregrounded.
        m._records.append(m._make_record(outcome="delivered",
                                         foreground={"hwnd": 1000, "process_name": "app.exe"}))
        m._records.append(m._make_record(outcome="delivered",
                                         foreground={"hwnd": 1000, "process_name": "app.exe"}))
        report = m.target_integrity()
        assert report["clean"] and report["checked"] == 2 and report["violations"] == 0

    def test_violation_when_an_event_landed_elsewhere(self):
        from frameforge.actions.safety import InputSafetyManager

        m = InputSafetyManager(target_hwnd=1000)
        m.arm()
        m._records.append(m._make_record(outcome="delivered",
                                         foreground={"hwnd": 1000, "process_name": "app.exe"}))
        m._records.append(m._make_record(
            outcome="delivered",
            foreground={"hwnd": 4242, "process_name": "Hermes.exe",
                        "class_name": "Chrome_WidgetWin_1"}))
        report = m.target_integrity()
        assert not report["clean"]
        assert report["violations"] == 1
        assert report["examples"][0]["actual_foreground_hwnd"] == 4242

    def test_blocked_events_are_not_violations(self):
        """A refused event is the guard working, not a violation."""
        from frameforge.actions.safety import InputSafetyManager

        m = InputSafetyManager(target_hwnd=1000)
        m._records.append(m._make_record(outcome="blocked", violation="forbidden_key",
                                         foreground={"hwnd": 9999}))
        assert m.target_integrity()["clean"]

    def test_no_target_bound_is_clean_by_construction(self):
        from frameforge.actions.safety import InputSafetyManager

        m = InputSafetyManager()
        report = m.target_integrity()
        assert report["clean"] and "no target" in report["note"]

    def test_integrity_is_part_of_health(self):
        from frameforge.actions.safety import InputSafetyManager

        m = InputSafetyManager(target_hwnd=1000)
        m.arm()
        m._records.append(m._make_record(outcome="delivered", foreground={"hwnd": 4242}))
        assert m.post_run_check()["healthy"] is False, (
            "a run that sent input off-target must not report healthy"
        )


# ------------------------------------------------- gap 2: identity verification


class TestTargetIdentity:
    """Adopted from Codex execpolicy: program identity needs an absolute path.

    Frame Forge's allow-list keyed on process name, which a process controls. Codex pins
    ``host_executable(paths=[...])`` and gates basename fallback, precisely because a
    name-only rule also matches a different program that calls itself the same thing.
    """

    def _real(self) -> str:
        return os.path.basename(os.sys.executable).replace(".exe", "") + ".exe"

    def test_real_process_with_image_path_verifies(self):
        target = ActionTarget(hwnd=1, pid=os.getpid(), process_name=self._real(),
                              image_path=os.path.abspath(os.sys.executable))
        ok, why = target.identity_ok()
        assert ok, why

    def test_unresolved_identity_is_refused(self):
        target = ActionTarget(hwnd=1, pid=1234, process_name="app.exe", image_path="")
        ok, why = target.identity_ok()
        assert not ok and "could not be resolved" in why

    def test_nonexistent_image_path_is_refused(self):
        target = ActionTarget(hwnd=1, pid=1, process_name="app.exe",
                              image_path=r"C:\nope\app.exe")
        ok, why = target.identity_ok()
        assert not ok and "does not exist" in why

    def test_basename_disagreement_is_refused(self):
        target = ActionTarget(hwnd=1, pid=1, process_name="notepad.exe",
                              image_path=os.path.abspath(os.sys.executable))
        ok, why = target.identity_ok()
        assert not ok and "disagrees" in why

    def test_expected_path_mismatch_is_refused(self):
        target = ActionTarget(hwnd=1, pid=os.getpid(), process_name=self._real(),
                              image_path=os.path.abspath(os.sys.executable))
        ok, why = target.identity_ok(r"C:\somewhere\else.exe")
        assert not ok and "expected" in why

    def test_expected_path_match_verifies(self):
        target = ActionTarget(hwnd=1, pid=os.getpid(), process_name=self._real(),
                              image_path=os.path.abspath(os.sys.executable))
        assert target.identity_ok(os.path.abspath(os.sys.executable))[0]

    def test_policy_refuses_an_unverified_identity(self):
        policy = InputPolicy(PolicyConfig(allow_processes=frozenset({"app.exe"})))
        request = ToolRequest(
            action=KeyPress(key=Key.A), action_id="a1",
            target=ActionTarget(hwnd=1, pid=1, process_name="app.exe", image_path=""),
        )
        result = policy.validate(request)
        assert not result.allowed
        assert result.reason is DenyReason.IDENTITY_UNVERIFIED


# ------------------------------------------------- gap 3: every refusal has a remedy


class TestDenialRemedies:
    """Adopted from execpolicy: a `forbidden` rule says what to use instead.

    A refusal that does not say what to do instead is a dead end for whoever hit it.
    """

    def test_every_reason_has_a_remedy(self):
        for reason in DenyReason:
            assert reason.remedy.strip(), f"{reason} has no remedy"
            assert len(reason.remedy) > 20, f"{reason}'s remedy is not actionable"

    def test_denial_detail_includes_the_remedy(self):
        policy = InputPolicy(PolicyConfig())
        result = policy.validate(ToolRequest(action=KeyPress(key=Key.A), action_id="a1"))
        assert not result.allowed
        assert "To proceed:" in result.detail

    def test_identity_denial_points_at_the_real_fix(self):
        remedy = DenyReason.IDENTITY_UNVERIFIED.remedy
        assert "image path" in remedy and "elevated" in remedy

    def test_right_click_denial_explains_the_rule(self):
        assert "RightClick" in DenyReason.RIGHT_CLICK.remedy

    def test_cleanup_denial_tells_the_user_the_command(self):
        assert "recover-input" in DenyReason.CLEANUP_FAILED.remedy


# ------------------------------------------------ gap 4: hardened child environment


class TestHardenedChildEnv:
    """Adopted from Codex's process-hardening crate.

    Frame Forge launches the target application, a PowerShell OCR host and ffmpeg. It was
    handing them its environment verbatim, so anything able to set an environment variable
    could subvert a child.
    """

    def test_unsafe_variables_are_stripped(self):
        from frameforge.actions.safety import UNSAFE_ENV_VARS, hardened_child_env

        env = hardened_child_env({"PATH": "x", "LD_PRELOAD": "/evil.so",
                                  "PYTHONPATH": "/evil", "NODE_OPTIONS": "--x"})
        assert "PATH" in env
        for name in UNSAFE_ENV_VARS:
            assert name not in env

    def test_current_process_env_is_never_mutated(self):
        from frameforge.actions.safety import hardened_child_env

        before = dict(os.environ)
        hardened_child_env()
        assert dict(os.environ) == before

    def test_explicit_extra_survives(self):
        """A caller that deliberately sets one has decided; do not silently override."""
        from frameforge.actions.safety import hardened_child_env

        env = hardened_child_env({"PATH": "x"}, extra={"PYTHONPATH": "/intended"})
        assert env["PYTHONPATH"] == "/intended"

    def test_hardened_env_is_imported_wherever_used(self):
        """A helper used without importing it fails only when that code path runs.

        Adding ``env=hardened_child_env()`` to the OCR host introduced a NameError that
        only appeared when the host started - so the OCR backend silently degraded to
        returning no text, and a Notepad run failed with a *perceptual* error that pointed
        nowhere near the cause. A helper call is a dependency, and CI should say so.
        """
        import ast

        src = Path(__file__).resolve().parents[2] / "src" / "frameforge"
        defining_module = "actions/safety.py"
        offenders = []
        for path in src.rglob("*.py"):
            if path.relative_to(src).as_posix() == defining_module:
                continue
            text = path.read_text(encoding="utf-8")
            if "hardened_child_env(" not in text:
                continue
            tree = ast.parse(text)
            imported = any(
                (isinstance(n, ast.ImportFrom) and any(
                    a.name == "hardened_child_env" for a in n.names))
                or (isinstance(n, ast.Import) and any(
                    a.name.endswith("hardened_child_env") for a in n.names))
                for n in ast.walk(tree)
            )
            if not imported:
                offenders.append(path.relative_to(src).as_posix())
        assert not offenders, (
            f"hardened_child_env used without import (each fails only at runtime): {offenders}"
        )

    def test_ocr_backend_is_importable_and_functional(self):
        """A degraded capability must not present as a perceptual failure.

        The missing-import regression made OCR return zero lines on every image, so a Notepad
        run reported "landmark not visible" - a perceptual error with a build cause. If the
        OCR backend cannot even be constructed, that must surface immediately rather than as
        a downstream misdiagnosis.
        """
        from frameforge.adapters.ocr.rapidocr_ocr import build_ocr

        ocr = build_ocr("winrt")
        caps = ocr.capabilities()
        assert caps.available, f"OCR backend unavailable: {caps.notes}"
        close = getattr(ocr, "close", None)
        if callable(close):
            close()

    def test_every_process_spawn_passes_a_hardened_env(self):
        """The point is coverage: an unhardened spawn site is a regression.

        Matches ``subprocess.Popen`` / ``subprocess.run`` specifically. An earlier version
        matched any ``.run()`` and flagged ``asyncio.run``, which is not a process spawn -
        a reminder that a structural check is only as good as the thing it distinguishes.
        """
        import ast

        src = Path(__file__).resolve().parents[2] / "src" / "frameforge"
        checked = 0
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "subprocess." not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in ("Popen", "run"):
                    continue
                # Confirm the receiver really is the subprocess module.
                base = ast.unparse(node.func.value)
                if base != "subprocess":
                    continue
                checked += 1
                rel = path.relative_to(src).as_posix()
                has_env = any(kw.arg == "env" for kw in node.keywords)
                assert has_env, (
                    f"{rel}:{node.lineno} spawns a process without a hardened environment"
                )
        assert checked >= 4, f"expected several spawn sites, found {checked}"
