"""Settings, paths, and the authorisation policy.

Everything the operator can influence lives here, and every value has a default that is
safe rather than convenient. In particular:

* ``ai.enabled`` defaults to **False**. The system is complete without AI, and the
  default should be the mode that needs no key, no network, and no cost
  (guardrail G-DEG-01).
* ``direct_launch`` defaults to **False**, and requires per-scenario opt-in on top of
  this, with the exact command recorded in the report (G-BLAST-02).
* ``grace_seconds`` defaults to 5. There is a countdown before a run may touch anything.

Settings resolve in order: explicit argument > environment (``FRAMEFORGE_*``) > config
file > default. Secrets live in environment variables only; the config file is not a
secret store and says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

ENV_PREFIX = "FRAMEFORGE_"

#: Enforcement modes permitted by guardrail G-AUTH-02. Anything else is rejected at load.
AUTHORIZED_USES: frozenset[str] = frozenset({
    "owner_prototype",
    "developer_authorized_qa",
    "offline_single_player",
    "private_sandbox",
    "supervised_accessibility",
})

#: Modes that must not be silent. G-AUTH-03: reliable offline determination is imperfect,
#: so the default is a loud warning in the report rather than a guess that could stop a
#: legitimate run or wave through an illegitimate one.
OFFLINE_ENFORCEMENT_MODES: frozenset[str] = frozenset({"warn", "enforce", "off"})


@dataclass(slots=True)
class Settings:
    """Runtime configuration."""

    # ------------------------------------------------------------------ paths
    runs_dir: Path = field(default_factory=lambda: Path("runs"))
    profiles_dir: Path = field(default_factory=lambda: Path("profiles"))
    tasks_dir: Path = field(default_factory=lambda: Path("tasks"))

    # --------------------------------------------------------------- capture
    capture_backend: str = "mss"          # "mss" | "dxgi"
    #: Target surface. "window" attaches to the resolved target; "monitor" captures the
    #: whole display the window is on; "desktop" captures everything (G-PER-04 requires
    #: this to be explicit).
    capture_scope: str = "window"
    monitor_index: int = 0
    capture_fps_limit: int = 12

    # -------------------------------------------------------------------- ocr
    ocr_backend: str = "auto"             # "auto" | "winrt" | "rapidocr" | "null"
    ocr_max_dimension: int = 1280

    # -------------------------------------------------------------------- ai
    ai_enabled: bool = False              # default OFF: complete without AI
    ai_provider: str = "none"             # "none" | "openai_compat" | "cli" | "recorded"
    ai_base_url: str = ""
    ai_model: str = ""
    #: Key comes from the environment, never from a file. G-PER-03.
    ai_api_key_env: str = "FRAMEFORGE_AI_API_KEY"
    ai_timeout_s: float = 30.0
    ai_max_plan_steps: int = 5
    #: When set, frames are attached to the AI packet. Off by default (G-PER-01).
    share_frames: bool = False

    # ---------------------------------------------------------------- launch
    launch_mode: str = "ui"               # "ui" (primary) | "direct" | "attach"
    allow_direct_launch: bool = False
    grace_seconds: float = 5.0

    # --------------------------------------------------------------- budgets
    max_actions: int = 2000
    max_actions_per_minute: int = 240
    max_ai_calls: int = 60
    max_unknown_states: int = 25
    max_run_ms: int = 3_600_000

    # --------------------------------------------------------------- safety
    focus_policy: str = "pause"           # "pause" | "abort" | "refocus"
    human_input_policy: str = "pause"     # "pause" | "resume" | "ignore"
    focus_tolerance_ms: float = 250.0
    estop_hotkey: str = "ctrl+alt+F12"
    min_confidence_to_act: float = 0.20
    offline_enforcement: str = "warn"

    # ------------------------------------------------------------- evidence
    evidence_max_mb: int = 200
    record_video: bool = False
    write_junit: bool = False
    keep_runs: int = 30

    # -------------------------------------------------------------- privacy
    redact_regions: tuple[tuple[float, float, float, float], ...] = ()
    include_ocr_text_in_report: bool = False

    # -------------------------------------------------------------- logging
    log_level: str = "INFO"
    verbose: bool = False

    # ------------------------------------------------------------------ misc
    seed: int = 0

    def __post_init__(self) -> None:
        for name in ("runs_dir", "profiles_dir", "tasks_dir"):
            setattr(self, name, Path(getattr(self, name)))
        self.validate()

    # ------------------------------------------------------------- validation

    def validate(self) -> None:
        """Reject nonsensical configuration at load, not mid-run."""
        if self.capture_backend not in ("mss", "dxgi", "auto"):
            msg = f"capture_backend must be mss|dxgi|auto, got {self.capture_backend!r}"
            raise ValueError(msg)
        if self.capture_scope not in ("window", "monitor", "desktop"):
            msg = f"capture_scope must be window|monitor|desktop, got {self.capture_scope!r}"
            raise ValueError(msg)
        if self.ocr_backend not in ("auto", "winrt", "rapidocr", "null"):
            msg = f"ocr_backend must be auto|winrt|rapidocr|null, got {self.ocr_backend!r}"
            raise ValueError(msg)
        if self.launch_mode not in ("ui", "direct", "attach"):
            msg = f"launch_mode must be ui|direct|attach, got {self.launch_mode!r}"
            raise ValueError(msg)
        if self.focus_policy not in ("pause", "abort", "refocus"):
            msg = f"focus_policy must be pause|abort|refocus, got {self.focus_policy!r}"
            raise ValueError(msg)
        if self.human_input_policy not in ("pause", "resume", "ignore"):
            msg = f"human_input_policy must be pause|resume|ignore, got {self.human_input_policy!r}"
            raise ValueError(msg)
        if self.offline_enforcement not in OFFLINE_ENFORCEMENT_MODES:
            msg = f"offline_enforcement must be warn|enforce|off, got {self.offline_enforcement!r}"
            raise ValueError(msg)
        if self.launch_mode == "direct" and not self.allow_direct_launch:
            msg = (
                "launch_mode='direct' requires allow_direct_launch=true. Direct launch is an "
                "explicit, opt-in QA optimisation and must not be reachable by default "
                "(docs/AI_GUARDRAILS.md G-BLAST-02)."
            )
            raise ValueError(msg)
        if not (0.0 <= self.min_confidence_to_act <= 1.0):
            msg = f"min_confidence_to_act must be in [0,1], got {self.min_confidence_to_act}"
            raise ValueError(msg)

    # ------------------------------------------------------------ conversion

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out

    def redacted_dict(self) -> dict[str, Any]:
        """Settings safe to write into a report.

        Excludes anything key-shaped and never includes a resolved secret - the API key is
        only ever read from the environment, at call time, by the client.
        """
        data = self.to_dict()
        data.pop("ai_api_key_env", None)
        data["ai_api_key_present"] = bool(os.environ.get(self.ai_api_key_env))
        return data

    def summary_lines(self) -> list[str]:
        return [
            f"capture      : {self.capture_backend} scope={self.capture_scope} "
            f"monitor={self.monitor_index}",
            f"ocr          : {self.ocr_backend} (max_dim={self.ocr_max_dimension})",
            f"ai           : enabled={self.ai_enabled} provider={self.ai_provider} "
            f"model={self.ai_model or '-'} key_present={bool(os.environ.get(self.ai_api_key_env))}",
            f"launch       : {self.launch_mode} direct_allowed={self.allow_direct_launch} "
            f"grace={self.grace_seconds:.0f}s",
            f"safety       : focus={self.focus_policy} human_input={self.human_input_policy} "
            f"estop={self.estop_hotkey}",
            f"budgets      : actions={self.max_actions} rate={self.max_actions_per_minute}/min "
            f"ai_calls={self.max_ai_calls} unknowns={self.max_unknown_states} "
            f"run={self.max_run_ms / 1000:.0f}s",
            f"confidence   : min_to_act={self.min_confidence_to_act:.2f}",
            f"evidence     : video={self.record_video} max={self.evidence_max_mb}MB "
            f"ocr_in_report={self.include_ocr_text_in_report}",
            f"paths        : runs={self.runs_dir} profiles={self.profiles_dir} tasks={self.tasks_dir}",
        ]


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, Path):
        return Path(value)
    return value


def load_settings(
    config_path: Path | None = None,
    **overrides: Any,
) -> Settings:
    """Resolve settings from defaults, file, environment, then explicit overrides."""
    data: dict[str, Any] = {}

    if config_path and Path(config_path).exists():
        raw = Path(config_path).read_text(encoding="utf-8")
        try:
            data.update(json.loads(raw))
        except json.JSONDecodeError as exc:
            msg = f"config file {config_path} is not valid JSON: {exc}"
            raise ValueError(msg) from exc

    # Environment: FRAMEFORGE_CAPTURE_BACKEND etc.
    base = Settings()
    known = {f.name for f in fields(Settings)}
    for name in known:
        env_key = ENV_PREFIX + name.upper()
        if env_key in os.environ:
            data[name] = _coerce(os.environ[env_key], getattr(base, name))

    for key, value in overrides.items():
        if value is not None:
            data[key] = value

    valid = {k: v for k, v in data.items() if k in known}
    unknown = set(data) - known
    if unknown:
        msg = f"unknown settings: {sorted(unknown)}"
        raise ValueError(msg)

    if "redact_regions" in valid:
        valid["redact_regions"] = tuple(tuple(r) for r in valid["redact_regions"])

    return Settings(**valid)


def ai_api_key(settings: Settings) -> str:
    """Read the API key from the environment at call time.

    Never from a file, never logged, never included in a report. Guardrail G-PER-03.
    """
    return os.environ.get(settings.ai_api_key_env, "")


__all__ = ["AUTHORIZED_USES", "Settings", "ai_api_key", "load_settings"]
