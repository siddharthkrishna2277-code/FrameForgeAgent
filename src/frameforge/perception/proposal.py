"""Target-scoped capture and the vision proposal contract.

The governing rule: **AI confidence is not authorization.** Vision may look at the target
and *propose* a point; it may never inject, expand an allow-listed region, alter policy, or
bypass the target gate. A proposal is a suggestion that must survive the same deterministic
validation as anything else.

Two consequences shape this module:

1. Every capture is bound to the target session that produced it. A frame from the desktop,
   or from a different window, is not evidence about the target.
2. A proposal is expressed in **target-client coordinates**, not screen coordinates. Screen
   coordinates are derived at the point of authorisation, from the live session geometry, so
   a proposal cannot carry a stale or fabricated screen position.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from frameforge.actions.arm import CaptureFrame
from frameforge.actions.coordinates import ScreenPx, SurfacePx, surface_to_screen


class CaptureHealth(StrEnum):
    OK = "ok"
    BLACK = "black"
    FROZEN = "frozen"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    SESSION_MISMATCH = "session_mismatch"


class ProposalType(StrEnum):
    """What a proposal asks for. Deliberately narrow; nothing here is a raw OS call."""

    CLICK_LEFT = "click_left"
    CLICK_RIGHT = "click_right"
    MOVE = "move"
    WAIT = "wait"
    VERIFY_ONLY = "verify_only"


@dataclass(frozen=True, slots=True)
class Region:
    """A rectangle in target-client coordinates, as fractions of the client area.

    Fractions rather than pixels so a proposal survives a resize. A resize between
    proposal and authorisation changes the mapping, which the freshness rules catch.
    """

    x: float
    y: float
    width: float
    height: float
    label: str = ""

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            msg = f"region must have positive extent, got {self.width}x{self.height}"
            raise ValueError(msg)

    def contains(self, p: SurfacePx, client_w: int, client_h: int) -> bool:
        x0 = self.x * client_w
        y0 = self.y * client_h
        return (x0 <= p.x < x0 + self.width * client_w
                and y0 <= p.y < y0 + self.height * client_h)

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "width": self.width,
                "height": self.height, "label": self.label}


@dataclass(frozen=True, slots=True)
class VisionProposal:
    """A declaration of intent produced by vision or a planner.

    This type has no way to express "inject input". It carries a *point in the target's
    client coordinates* and an expectation; authorisation is somebody else's decision.
    """

    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    frame_id: str = ""
    session_id: str = ""
    type: ProposalType = ProposalType.VERIFY_ONLY
    #: What the proposal believes it is looking at, for the operator to check.
    target_description: str = ""
    #: Client-relative. Converted to screen coordinates only at authorisation time.
    point_in_target_client: SurfacePx | None = None
    region: Region | None = None
    confidence: float = 0.0
    visible: bool = False
    occluded: bool = False
    reasoning_summary: str = ""
    expected_outcome: str = ""
    created_mono_ms: float = field(default_factory=lambda: time.monotonic() * 1000.0)
    max_age_ms: float = 5000.0
    #: Populated at authorisation, never by the proposer.
    authorized: bool = False
    refusal: str = ""

    @property
    def age_ms(self) -> float:
        return time.monotonic() * 1000.0 - self.created_mono_ms

    @property
    def stale(self) -> bool:
        return self.age_ms > self.max_age_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id, "frame_id": self.frame_id,
            "session_id": self.session_id, "type": self.type.value,
            "target_description": self.target_description,
            "point_in_target_client": (self.point_in_target_client.to_point().as_tuple()
                                       if self.point_in_target_client else None),
            "region": self.region.to_dict() if self.region else None,
            "confidence": round(self.confidence, 3), "visible": self.visible,
            "occluded": self.occluded, "reasoning_summary": self.reasoning_summary,
            "expected_outcome": self.expected_outcome,
            "age_ms": round(self.age_ms), "stale": self.stale,
            "authorized": self.authorized, "refusal": self.refusal,
        }


class ProposalError(RuntimeError):
    """A proposal was malformed or bound to the wrong session."""


class TargetCapture:
    """Capture bound to one registered target session.

    Wraps the existing capture adapter with session identity and freshness, so a frame can
    never be silently attributed to the wrong window. The underlying adapter is unchanged;
    what is added is the binding and the staleness rule.
    """

    def __init__(self, session, capture_adapter, vision=None) -> None:
        self.session = session
        self._capture = capture_adapter
        self._vision = vision
        self.frames: list[CaptureFrame] = []

    def grab(self, run_id: str = "", *, max_age_ms: float = 750.0) -> CaptureFrame | None:
        """Capture the registered target and bind the frame to the session."""
        if self.session is None:
            return None
        hwnd = int(getattr(self.session, "hwnd", 0) or 0)
        pid = int(getattr(self.session, "pid", 0) or 0)
        if not hwnd:
            return None

        # The window must still be the one we registered, or the capture would describe a
        # different application entirely.
        from frameforge.actions.target import window_identity

        live = window_identity(hwnd)
        if not live or int(live.get("pid", 0)) != pid:
            frame = CaptureFrame(
                frame_id=uuid.uuid4().hex[:12], run_id=run_id,
                session_id=getattr(self.session, "session_id", ""),
                target_hwnd=hwnd, target_pid=pid,
                captured_mono_ms=time.monotonic() * 1000.0,
                healthy=False,
                health_detail="target window is gone or belongs to another process",
                source="windows",
            )
            self.frames.append(frame)
            return frame

        raw = self._capture.grab()
        if raw is None:
            frame = CaptureFrame(
                frame_id=uuid.uuid4().hex[:12], run_id=run_id,
                session_id=getattr(self.session, "session_id", ""),
                target_hwnd=hwnd, target_pid=pid,
                captured_mono_ms=time.monotonic() * 1000.0,
                healthy=False, health_detail="capture returned no frame", source="windows",
            )
            self.frames.append(frame)
            return frame

        surface = raw.surface
        client = getattr(self.session, "client_rect", None)
        frame = CaptureFrame(
            frame_id=uuid.uuid4().hex[:12],
            run_id=run_id,
            session_id=getattr(self.session, "session_id", ""),
            target_hwnd=hwnd,
            target_pid=pid,
            captured_mono_ms=time.monotonic() * 1000.0,
            max_age_ms=max_age_ms,
            width=int(raw.array.shape[1]),
            height=int(raw.array.shape[0]),
            client_rect=(client.x, client.y, client.width, client.height) if client else (0, 0, 0, 0),
            window_rect=(surface.offset_x, surface.offset_y,
                         surface.size.width, surface.size.height),
            monitor_index=int(getattr(self.session, "monitor_index", 0) or 0),
            monitor_device=getattr(self.session, "monitor_device", ""),
            dpi=int(getattr(self.session, "dpi", 96) or 96),
            topology_fingerprint=getattr(self.session, "topology_fingerprint", ""),
            source="windows",
        )
        frame.healthy, frame.health_detail = self._assess(raw)
        self.frames.append(frame)
        return frame

    def _assess(self, raw) -> tuple[bool, str]:
        """Black or frozen captures are marked unhealthy, because perception on them is a lie."""
        if self._vision is None:
            return True, ""
        try:
            if self._vision.is_black(raw.array):
                return False, "capture is black"
        except Exception:
            pass
        if len(self.frames) >= 2:
            try:
                previous = self._pending_array
            except AttributeError:
                previous = None
            if previous is not None:
                try:
                    diff = self._vision.diff(previous, raw.array).score
                    if diff == 0.0:
                        return False, "capture is frozen (identical to the previous frame)"
                except Exception:
                    pass
        self._pending_array = raw.array
        return True, ""

    def latest(self) -> CaptureFrame | None:
        return self.frames[-1] if self.frames else None

    def is_fresh(self, max_age_ms: float = 750.0) -> tuple[bool, str]:
        """Usable *and* bound to the current target.

        A fresh frame of the wrong window is worse than no frame: it looks like evidence
        and is not. The hwnd/pid/session binding is re-checked here so a target that was
        replaced or moved cannot be judged from a stale capture of its predecessor.
        """
        frame = self.latest()
        if frame is None:
            return False, "no capture yet"
        if frame.age_ms > max_age_ms:
            return False, f"latest capture is {frame.age_ms:.0f}ms old (max {max_age_ms:.0f}ms)"
        if not frame.healthy:
            return False, frame.health_detail or "capture unhealthy"

        hwnd = int(getattr(self.session, "hwnd", 0) or 0)
        pid = int(getattr(self.session, "pid", 0) or 0)
        if hwnd and (frame.target_hwnd != hwnd or frame.target_pid != pid):
            return False, (
                f"capture is for hwnd={frame.target_hwnd} pid={frame.target_pid}, "
                f"target is hwnd={hwnd} pid={pid}"
            )
        expected_fp = getattr(self.session, "topology_fingerprint", "")
        if expected_fp and frame.topology_fingerprint and \
                frame.topology_fingerprint != expected_fp:
            return False, "capture predates the current display topology"
        return True, ""


def to_screen(proposal: VisionProposal, session) -> ScreenPx:
    """Convert a proposal's client point to virtual-desktop physical coordinates.

    Done here, at authorisation time, from live session geometry - never in the proposal.
    A proposer that could state a screen coordinate could state a fabricated one.
    """
    if proposal.point_in_target_client is None:
        msg = "proposal carries no client point"
        raise ProposalError(msg)
    origin = getattr(session, "client_origin", (0, 0))
    return surface_to_screen(proposal.point_in_target_client, origin)


def verify_proposal(
    proposal: VisionProposal,
    session,
    *,
    require_visible: bool = True,
) -> tuple[bool, str]:
    """Cheap pre-checks on a proposal. Necessary, never sufficient.

    Even a perfect proposal still goes through the state machine and the target guard; this
    only avoids wasting an authorisation round-trip on an obviously dead proposal.
    """
    if session is None:
        return False, "no target session"
    session_id = getattr(session, "session_id", "")
    if proposal.session_id and session_id and proposal.session_id != session_id:
        return False, (f"proposal is bound to session {proposal.session_id}, "
                       f"target is {session_id}")
    if proposal.stale:
        return False, f"proposal is {proposal.age_ms:.0f}ms old"
    if require_visible and not proposal.visible:
        return False, "proposal does not claim the element is visible"
    if proposal.occluded:
        return False, "proposal reports the element is occluded"
    if proposal.point_in_target_client is None and proposal.type in (
        ProposalType.CLICK_LEFT, ProposalType.CLICK_RIGHT, ProposalType.MOVE
    ):
        return False, "click/move proposal carries no client point"
    if proposal.confidence < 0.5:
        return False, f"proposal confidence {proposal.confidence:.2f} is too low to act on"
    return True, ""


__all__ = [
    "CaptureHealth",
    "ProposalError",
    "ProposalType",
    "Region",
    "TargetCapture",
    "VisionProposal",
    "to_screen",
    "verify_proposal",
]
