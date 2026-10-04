"""Profile loading with template resolution and fail-closed authorisation.

Loading is strict on purpose. A malformed profile is a *refusal*, not a warning: a
half-loaded profile that silently loses a landmark produces a run that does the wrong
thing confidently, which is the failure mode this whole system is built to avoid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from frameforge.ports.geometry import Rect, Size
from frameforge.profiles.schema import GameProfileSpec, LandmarkSpec, LauncherProfileSpec
from frameforge.kernel.errors import AuthorizationError, PolicyError


def load_game_profile(path: Path) -> GameProfileSpec:
    """Parse and validate a game profile. Raises on any problem."""
    path = Path(path)
    if not path.exists():
        msg = f"profile not found: {path}"
        raise FileNotFoundError(msg)
    raw = _read_structured(path)
    try:
        spec = GameProfileSpec.model_validate(raw)
    except Exception as exc:
        msg = f"invalid game profile {path}: {exc}"
        raise PolicyError(msg) from exc
    _assert_authorised(spec, path)
    return spec


def load_launcher_profile(path: Path) -> LauncherProfileSpec:
    path = Path(path)
    if not path.exists():
        msg = f"launcher profile not found: {path}"
        raise FileNotFoundError(msg)
    try:
        return LauncherProfileSpec.model_validate(_read_structured(path))
    except Exception as exc:
        msg = f"invalid launcher profile {path}: {exc}"
        raise PolicyError(msg) from exc


def _read_structured(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            msg = (
                "YAML profiles need PyYAML. Install it, or convert the profile to JSON - "
                "JSON is the format with no dependency."
            )
            raise PolicyError(msg) from None
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        msg = f"profile {path} must contain an object at the top level"
        raise PolicyError(msg)
    return data


def _assert_authorised(spec: GameProfileSpec, path: Path) -> None:
    """Refuse an unattested profile.

    pydantic already enforces structure, so reaching here means an attestation exists but
    something is off - an implausibly short note, for instance. Cheap to check, and a
    second gate means a future schema relaxation cannot silently open this.
    """
    use = spec.authorized_use
    if use.basis not in (
        "owner_prototype",
        "developer_authorized_qa",
        "offline_single_player",
        "private_sandbox",
        "supervised_accessibility",
    ):
        msg = f"profile {path} declares unsupported basis {use.basis!r}"
        raise AuthorizationError(msg)
    if len(use.attestation.strip()) < 8:
        msg = f"profile {path} attestation is too short to be meaningful"
        raise AuthorizationError(msg)
    if use.basis == "offline_single_player" and not use.offline:
        msg = (
            f"profile {path} declares offline_single_player but offline=false; that is "
            "internally inconsistent"
        )
        raise AuthorizationError(msg)


# --------------------------------------------------------------- template loading


def load_templates(spec: GameProfileSpec, *, vision=None) -> dict[str, np.ndarray]:
    """Load landmark templates from disk, relative to the profile file.

    Accepts a ``base_dir`` so a profile can be loaded without knowing where it lives.
    Missing templates are reported, not skipped silently: a landmark that cannot load is a
    landmark that will always evaluate absent, and that shows up as a mystery UNKNOWN much
    later.
    """
    import cv2

    base = spec.metadata.get("_profile_dir", ".")
    out: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for landmark in spec.landmarks:
        if landmark.kind != "template" or not landmark.template:
            continue
        candidate = Path(base) / landmark.template
        if not candidate.exists():
            missing.append(f"{landmark.name} -> {candidate}")
            continue
        data = np.fromfile(str(candidate), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            missing.append(f"{landmark.name} -> unreadable image {candidate}")
            continue
        out[landmark.name] = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    if missing:
        msg = f"profile {spec.name!r}: unreadable templates: {missing}"
        raise PolicyError(msg)
    return out


def resolve_landmark_region(landmark: LandmarkSpec, surface: Size) -> Rect | None:
    """Absolute rect for a landmark's declared region, clamped to the surface."""
    if landmark.region is None:
        return None
    return landmark.region.to_rect(surface)


def landmark_click_point(rect: Rect, landmark: LandmarkSpec) -> tuple[int, int]:
    """Deterministic click point within a matched landmark.

    Anchored to the landmark's own rect rather than to screen coordinates, so it follows
    the element wherever it is - including on the second monitor, which is exactly the
    case that hard-coded coordinates get wrong.
    """
    ox, oy = landmark.click_offset
    return (
        int(rect.x + rect.width * ox),
        int(rect.y + rect.height * oy),
    )


def discover_profiles(profiles_dir: Path) -> dict[str, Path]:
    """Index profiles by name. Later duplicates are ignored, not silently winning."""
    out: dict[str, Path] = {}
    root = Path(profiles_dir)
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        if path.stem in out:
            continue
        out[path.stem] = path
    return out


__all__ = [
    "discover_profiles",
    "landmark_click_point",
    "load_game_profile",
    "load_launcher_profile",
    "load_templates",
    "resolve_landmark_region",
]
