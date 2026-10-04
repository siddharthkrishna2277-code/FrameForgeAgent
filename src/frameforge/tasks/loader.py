"""Task / scenario loading from JSON or YAML.

The DSL is data. A scenario author is a game developer, not necessarily a Python
programmer, so the format is readable and the failure messages say which field is wrong
rather than raising a pydantic traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frameforge.kernel.errors import PolicyError
from frameforge.tasks.dsl import (
    EvidencePolicy,
    LaunchSpec,
    OnUnreachable,
    ScenarioDefinition,
    StepAction,
    StepKind,
    TaskDefinition,
    TaskStep,
)


def load_task(path: Path) -> ScenarioDefinition:
    """Load a scenario. Always returns a ScenarioDefinition (a task is a degenerate one)."""
    path = Path(path)
    if not path.exists():
        msg = f"task not found: {path}"
        raise FileNotFoundError(msg)
    raw = _read(path)
    try:
        return parse_scenario(raw, source=str(path))
    except PolicyError:
        raise
    except Exception as exc:
        msg = f"invalid task {path}: {exc}"
        raise PolicyError(msg) from exc


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            msg = "YAML tasks need PyYAML; use a .json task instead"
            raise PolicyError(msg) from None
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        msg = f"task {path} must contain an object at the top level"
        raise PolicyError(msg)
    return data


def parse_scenario(raw: dict[str, Any], source: str = "<memory>") -> ScenarioDefinition:
    """Build a ScenarioDefinition from a plain dict.

    Written by hand rather than via pydantic so each error can name the offending field in
    terms a scenario author will understand.
    """
    steps = tuple(_parse_step(s, i, source) for i, s in enumerate(raw.get("steps") or []))
    setup = tuple(
        _parse_step(s, i, source) for i, s in enumerate(raw.get("setup") or [])
    )
    assertions = tuple(
        _parse_step(a, i, source, is_assertion=True)
        for i, a in enumerate(raw.get("assertions") or [])
    )
    evidence_raw = raw.get("evidence") or {}
    known_evidence = {f for f in EvidencePolicy.__dataclass_fields__}
    unknown = set(evidence_raw) - known_evidence
    if unknown:
        msg = f"{source}: unknown evidence fields: {sorted(unknown)}"
        raise PolicyError(msg)
    evidence = EvidencePolicy(**evidence_raw)

    scenario_id = str(raw.get("scenario_id") or Path(source).stem)
    return ScenarioDefinition(
        name=str(raw.get("name") or scenario_id),
        objective=str(raw.get("objective", "")),
        description=str(raw.get("description", "")),
        steps=steps,
        setup=setup,
        assertions=assertions,
        tags=tuple(raw.get("tags") or ()),
        seed=int(raw.get("seed", 0)),
        evidence=evidence,
        metadata=dict(raw.get("metadata") or {}),
        allow_direct_launch=bool(raw.get("allow_direct_launch", False)),
        allow_irreversible=bool(raw.get("allow_irreversible", False)),
        launch=_parse_launch(raw.get("launch"), source),
        scenario_id=scenario_id,
        build_id=str(raw.get("build_id", "")),
        commit=str(raw.get("commit", "")),
        environment=dict(raw.get("environment") or {}),
        launch_mode=str(raw.get("launch_mode", "ui")),
        direct_launch_command=tuple(raw.get("direct_launch_command") or ()),
        expected_failures=tuple(raw.get("expected_failures") or ()),
        requires=tuple(raw.get("requires") or ()),
    )


def _parse_launch(raw: Any, source: str) -> LaunchSpec | None:
    """Parse a launch declaration, or return None.

    ``executable`` and ``args`` stay separate: nothing is ever concatenated into a shell
    string, which is where quoting bugs and command injection live.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        msg = f"{source}: launch must be an object"
        raise PolicyError(msg)
    executable = str(raw.get("executable", "")).strip()
    if not executable:
        msg = f"{source}: launch.executable is required"
        raise PolicyError(msg)
    args = raw.get("args") or []
    if not isinstance(args, list):
        msg = f"{source}: launch.args must be a list"
        raise PolicyError(msg)
    return LaunchSpec(
        executable=executable,
        args=tuple(str(a) for a in args),
        ready_landmark=str(raw.get("ready_landmark", "")),
        timeout_ms=int(raw.get("timeout_ms", 20_000)),
        pre_focus_target=str(raw.get("pre_focus_target", "")),
        rationale=str(raw.get("rationale", "")),
    )


def _parse_step(raw: dict[str, Any], index: int, source: str, *, is_assertion: bool = False) -> TaskStep:
    where = f"{source}: step[{index}]"
    if not isinstance(raw, dict):
        msg = f"{where} must be an object"
        raise PolicyError(msg)
    name = str(raw.get("name") or f"step_{index}")
    where = f"{source}: step {name!r}"

    kind_raw = str(raw.get("kind") or ("assert" if is_assertion else "interact"))
    try:
        kind = StepKind(kind_raw)
    except ValueError:
        msg = f"{where}: unknown kind {kind_raw!r}; valid: {[k.value for k in StepKind]}"
        raise PolicyError(msg) from None

    action = None
    if raw.get("action") is not None:
        action = _parse_action(raw["action"], where)

    try:
        on_unreachable = OnUnreachable(str(raw.get("on_unreachable", "fail")))
    except ValueError:
        msg = f"{where}: on_unreachable must be fail|skip|unknown"
        raise PolicyError(msg) from None

    verify_args = raw.get("verify_args") or {}
    if not isinstance(verify_args, dict):
        msg = f"{where}: verify_args must be an object"
        raise PolicyError(msg)

    return TaskStep(
        name=name,
        kind=kind,
        description=str(raw.get("description", "")),
        require=tuple(raw.get("require") or ()),
        expect=tuple(raw.get("expect") or ()),
        verify=str(raw.get("verify", "screen_changed")),
        verify_args=dict(verify_args),
        action=action,
        planner_action=str(raw.get("planner_action", "")),
        attempts=int(raw.get("attempts", 2)),
        timeout_ms=int(raw.get("timeout_ms", 30_000)),
        settle_ms=int(raw.get("settle_ms", 400)),
        on_unreachable=on_unreachable,
        tags=tuple(raw.get("tags") or ()),
        reach_timeout_ms=int(raw.get("reach_timeout_ms", 8_000)),
        retryable=raw.get("retryable"),
    )


def _parse_action(raw: dict[str, Any], where: str) -> StepAction:
    if not isinstance(raw, dict):
        msg = f"{where}: action must be an object"
        raise PolicyError(msg)
    kind = str(raw.get("kind", "intent"))
    at = raw.get("at")
    if at is not None and (
        not isinstance(at, list | tuple) or len(at) != 2
    ):
        msg = f"{where}: action.at must be [x, y]"
        raise PolicyError(msg)
    keys = raw.get("keys") or ()
    if not isinstance(keys, list | tuple):
        msg = f"{where}: action.keys must be a list"
        raise PolicyError(msg)
    valid = {
        "intent", "key", "hotkey", "click", "click_anchor", "click_anchor_text",
        "scroll", "mouselook", "type", "none",
    }
    if kind not in valid:
        msg = f"{where}: unknown action kind {kind!r}; valid: {sorted(valid)}"
        raise PolicyError(msg)
    if kind == "intent" and not raw.get("intent"):
        msg = f"{where}: action kind 'intent' requires an 'intent' field"
        raise PolicyError(msg)
    if kind == "key" and not raw.get("key"):
        msg = f"{where}: action kind 'key' requires a 'key' field"
        raise PolicyError(msg)
    if kind == "hotkey" and not keys:
        msg = f"{where}: action kind 'hotkey' requires 'keys'"
        raise PolicyError(msg)
    if kind in ("click_anchor", "click_anchor_text") and not raw.get("anchor"):
        msg = f"{where}: action kind {kind!r} requires an 'anchor'"
        raise PolicyError(msg)
    return StepAction(
        kind=kind,
        intent=str(raw.get("intent", "")),
        key=str(raw.get("key", "")),
        keys=tuple(str(k) for k in keys),
        at=tuple(int(v) for v in at) if at is not None else None,
        anchor=str(raw.get("anchor", "")),
        button=str(raw.get("button", "left")),
        count=int(raw.get("count", 1)),
        hold_ms=int(raw.get("hold_ms", 0)),
        text=str(raw.get("text", "")),
        scroll_x=int(raw.get("scroll_x", 0)),
        scroll_y=int(raw.get("scroll_y", 0)),
        dx=int(raw.get("dx", 0)),
        dy=int(raw.get("dy", 0)),
        steps=int(raw.get("steps", 12)),
        duration_ms=int(raw.get("duration_ms", 120)),
        idempotent=bool(raw.get("idempotent", True)),
    )


__all__ = ["load_task", "parse_scenario"]
