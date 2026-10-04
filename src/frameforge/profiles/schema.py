"""Profile schema: the authorisation boundary and the place game knowledge lives.

Guardrail G-DEV-01: no game names, coordinates, or key bindings may appear in the engine.
They live here, as validated data.

The authorisation block is the most important part of this file. A profile without a
valid ``authorized_use`` is *rejected at load time* - not warned about, not defaulted.
Attaching to a game is the highest-consequence thing this system does, and an implicit
"probably fine" default would make that consequence easy to reach by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from frameforge.config.settings import AUTHORIZED_USES
from frameforge.ports.geometry import RegionSpec
from frameforge.ports.vision import ColorProbe
from frameforge.ports.window import TargetSpec


class AuthorizedUse(BaseModel):
    """Owner attestation of the basis for automating this target.

    The system records the claim and its provenance; it does not attempt to verify that
    the claim is true, and never presents an unverified claim as verified
    (guardrail G-AUTH-05).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal[
        "owner_prototype",
        "developer_authorized_qa",
        "offline_single_player",
        "private_sandbox",
        "supervised_accessibility",
    ]
    #: Free-text note: what exactly the owner asserts, and under what circumstances.
    attestation: str = Field(min_length=8, max_length=600)
    attested_by: str = Field(default="owner", max_length=80)
    offline: bool = True
    #: Date the attestation was made; recorded for audit.
    as_of: str = ""

    def describe(self) -> str:
        return f"{self.basis} by {self.attested_by}"


class LandmarkSpec(BaseModel):
    """One visual landmark. Templates are loaded from disk by path, never inlined."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    kind: Literal["template", "text", "color", "activity"] = "template"
    #: Path to a PNG, relative to the profile file.
    template: str | None = None
    pattern: str = ""                 # regex, for kind="text"
    probe: ColorProbe | None = None   # for kind="color"
    region: RegionSpec | None = None
    threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    #: Optional deterministic click target for this landmark: a point relative to the
    #: matched rect. The centre by default.
    click_offset: tuple[float, float] = (0.5, 0.5)
    required: bool = False
    description: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> LandmarkSpec:
        if self.kind == "template" and not self.template:
            msg = f"landmark {self.name!r} of kind 'template' needs a template path"
            raise ValueError(msg)
        if self.kind == "text" and not self.pattern:
            msg = f"landmark {self.name!r} of kind 'text' needs a regex pattern"
            raise ValueError(msg)
        if self.kind == "color" and self.probe is None:
            msg = f"landmark {self.name!r} of kind 'color' needs a probe"
            raise ValueError(msg)
        if not (0.0 <= self.click_offset[0] <= 1.0) or not (0.0 <= self.click_offset[1] <= 1.0):
            msg = f"landmark {self.name!r} click_offset must be within [0,1]"
            raise ValueError(msg)
        return self


class SignalSpec(BaseModel):
    """An authorized non-visual signal, for asserting state that is not on screen.

    This is the sanctioned seam between Frame Forge and the game under test: a developer
    adds a log line or a status endpoint to their own QA build, declares it here, and the
    scenario can assert on it. Typescript and pasted text are never touched.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    #: "log" (tail a file), "json" (read a JSON file), "http" (GET a local URL).
    kind: Literal["log", "json", "http"] = "json"
    path: str = ""
    url: str = ""
    #: Regex with one capture group for "log", or a dotted key for "json".
    key: str = ""
    numeric: bool = False
    poll_ms: int = Field(default=250, ge=50, le=10_000)
    description: str = ""


class GameProfileSpec(BaseModel):
    """Everything Frame Forge knows about one target.

    Note what is absent: no source paths, no build steps, no patch instructions. A profile
    describes how to *operate and observe* a running window, and nothing else
    (guardrail G-ROLE-01, G-ROLE-03).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    name: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    display_name: str = ""
    description: str = ""

    # ------------------------------------------------------------ authorisation
    authorized_use: AuthorizedUse

    # ------------------------------------------------------------------ target
    target: TargetSpec
    control_preset: str = "ui"
    control_overrides: dict[str, Any] = Field(default_factory=dict)
    #: Reference resolution for viewport maths. ROIs are normalised, so this is used for
    #: aspect handling, not for coordinates.
    reference_size: tuple[int, int] = (1920, 1080)
    viewport: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    look_sensitivity: float | None = None
    #: How keystrokes are delivered to this target: ``unicode`` posts character events,
    #: ``scan`` posts scancodes. This is a genuine per-application property, not a global
    #: preference - the ``ui`` preset defaults to unicode, which a Win32 EDIT control
    #: ignores, so a Notepad-style profile must declare ``scan``.
    text_method: str | None = None

    # --------------------------------------------------------------- perception
    landmarks: tuple[LandmarkSpec, ...] = ()
    signals: tuple[SignalSpec, ...] = ()
    #: Ready-state landmarks: if none is visible, the target is not ready yet.
    ready_landmarks: tuple[str, ...] = ()
    loading_landmarks: tuple[str, ...] = ()

    # ------------------------------------------------------------------ actions
    #: Intents this profile permits. An allowlist, so a profile cannot accidentally permit
    #: everything.
    allowed_intents: tuple[str, ...] = ()
    forbidden_intents: tuple[str, ...] = ()
    allow_irreversible: bool = False

    # ------------------------------------------------------------------ policy
    #: Denylist check. Belt and braces alongside authorised_use.
    denied_process_names: tuple[str, ...] = ()
    #: Optional absolute path the target executable is expected to have.
    #:
    #: Added after studying how OpenAI Codex pins program identity in its execpolicy: a rule
    #: keyed on a program *name* also matches a different program that merely calls itself
    #: the same thing. A profile that knows where its target lives can have that verified
    #: before any input is sent. When empty, the runtime still resolves the image path and
    #: checks it exists and agrees with the reported name - just not against a fixed
    #: expectation, which is weaker but better than nothing.
    expected_image_path: str = ""

    # ------------------------------------------------------------------ reports
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reference_size")
    @classmethod
    def _positive_ref(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] <= 0 or v[1] <= 0:
            msg = f"reference_size must be positive, got {v}"
            raise ValueError(msg)
        return v

    @field_validator("text_method")
    @classmethod
    def _valid_text_method(cls, v: str | None) -> str | None:
        if v is not None and v not in ("unicode", "scan"):
            msg = f"text_method must be 'unicode' or 'scan', got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("viewport")
    @classmethod
    def _valid_viewport(cls, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x, y, w, h = v
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
            msg = f"viewport must be normalised within [0,1], got {v}"
            raise ValueError(msg)
        if x + w > 1.0001 or y + h > 1.0001:
            msg = f"viewport overflows the surface: {v}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _consistent(self) -> GameProfileSpec:
        if self.authorized_use.basis not in AUTHORIZED_USES:
            msg = f"authorised basis {self.authorized_use.basis!r} is not permitted"
            raise ValueError(msg)
        # Internally inconsistent attestations are refused here rather than only in the
        # loader, so no code path can construct an offline_single_player profile that
        # claims to be online.
        if self.authorized_use.basis == "offline_single_player" and not self.authorized_use.offline:
            msg = (
                "profile declares basis 'offline_single_player' but offline=false; that is "
                "internally inconsistent"
            )
            raise ValueError(msg)
        if len(self.authorized_use.attestation.strip()) < 8:
            msg = "authorized_use.attestation is too short to be a meaningful attestation"
            raise ValueError(msg)
        if not self.target.identifying:
            msg = (
                f"profile {self.name!r} has an unidentified target; a profile must say which "
                "window it operates (title_regex, class_name, process_name, pid or exe_names)"
            )
            raise ValueError(msg)
        names = [lm.name for lm in self.landmarks]
        if len(names) != len(set(names)):
            msg = f"duplicate landmark names in {self.name!r}: {names}"
            raise ValueError(msg)
        overlap = set(self.allowed_intents) & set(self.forbidden_intents)
        if overlap:
            msg = f"intents both allowed and forbidden: {sorted(overlap)}"
            raise ValueError(msg)
        signal_names = [s.name for s in self.signals]
        if len(signal_names) != len(set(signal_names)):
            msg = f"duplicate signal names in {self.name!r}: {signal_names}"
            raise ValueError(msg)
        for s in self.signals:
            if s.kind == "log" and not s.path:
                msg = f"signal {s.name!r} of kind 'log' needs a path"
                raise ValueError(msg)
            if s.kind == "json" and not (s.path or s.key):
                msg = f"signal {s.name!r} of kind 'json' needs path and key"
                raise ValueError(msg)
            if s.kind == "http" and not s.url:
                msg = f"signal {s.name!r} of kind 'http' needs a url"
                raise ValueError(msg)
            if s.kind == "http" and not s.url.startswith(("http://127.0.0.1", "http://localhost")):
                # Guardrail G-AUTH-01 spirit: signals must not be a channel out.
                msg = (
                    f"signal {s.name!r} url must be loopback; a remote endpoint would turn a "
                    "test hook into an exfiltration path"
                )
                raise ValueError(msg)
        known = set(names)
        for ref in (*self.ready_landmarks, *self.loading_landmarks):
            if ref not in known:
                msg = f"referenced landmark {ref!r} is not declared in {self.name!r}"
                raise ValueError(msg)
        return self

    def landmark(self, name: str) -> LandmarkSpec | None:
        return next((lm for lm in self.landmarks if lm.name == name), None)

    def signal(self, name: str) -> SignalSpec | None:
        return next((s for s in self.signals if s.name == name), None)


class LauncherProfileSpec(BaseModel):
    """How to reach and start a title through a visible UI.

    Not a special subsystem: a launcher is just a task that ends in "the game window is
    ready". Modelling it as a profile keeps one pipeline for both, and keeps direct
    launch from becoming a privileged path.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    name: str = Field(pattern=r"^[a-z0-9_]{1,48}$")
    display_name: str = ""
    #: Window of the launcher itself.
    launcher_target: TargetSpec
    #: Landmarks used to navigate the launcher's UI.
    landmarks: tuple[LandmarkSpec, ...] = ()
    #: Landmarks that indicate a visible click target exists.
    play_landmarks: tuple[str, ...] = ()
    #: Landmarks that indicate a dialog/loading state is on screen.
    dialog_landmarks: tuple[str, ...] = ()
    dismiss_strategy: str = "escape_then_click"
    max_navigation_steps: int = 12
    #: Opt-in only, and the command is recorded in the report (G-BLAST-02).
    direct_command: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> LauncherProfileSpec:
        if not self.launcher_target.identifying:
            msg = f"launcher profile {self.name!r} must identify the launcher window"
            raise ValueError(msg)
        if self.direct_command:
            msg = (
                f"launcher profile {self.name!r} declares a direct_command. Direct launch is "
                "opt-in per scenario and must not be baked into a profile "
                "(docs/AI_GUARDRAILS.md G-BLAST-02)."
            )
            raise ValueError(msg)
        return self


__all__ = ["AuthorizedUse", "GameProfileSpec", "LauncherProfileSpec", "LandmarkSpec", "SignalSpec"]
