"""Fault-domain classification, gated on evidence.

A failed verification tells you *that* a postcondition was not observed. It does not tell
you *why*, and the two possible whys call for completely different remediation:

===========================  ==========================================================
apparent result              what the evidence frame shows
===========================  ==========================================================
OCR cannot find the text      the text IS visible in the document
                              -> perception fault: OCR, UIA extraction, region
                                 selection, verifier thresholds
OCR cannot find the text      the text is ABSENT from the document
                              -> delivery/state fault: input routing, focus, target
                                 authority, event semantics, editor behaviour
===========================  ==========================================================

Conflating these is how the Notepad round-trip was misdiagnosed. Its report recorded a
perception failure while the typed text had in fact never reached the screen - a defect in
the input path, found only by opening the saved evidence frame.

So classification is not offered here as a free-floating label. ``classify_failure``
refuses to name a domain unless it is given the observation that proves the state, and a
verifier cannot supply that for a text condition by itself. Absent evidence, the answer is
``UNDETERMINED`` and says what artifact would settle it.

This module has no opinion about *how* to look at a frame. That is a visu's job. Its only
job is to refuse to guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FaultDomain(StrEnum):
    """Where a failure actually lives. Ordered from most to least specific."""

    #: The asserted postcondition was visible; the verifier failed to read it.
    PERCEPTION = "perception"

    #: The asserted postcondition was not on screen; the action never took effect.
    DELIVERY = "delivery"

    #: The action did land, but the application reached a different state than intended
    #: (a dialog opened, a step was rejected, the target changed underneath).
    APPLICATION_STATE = "application_state"

    #: The run was stopped by a guard rather than by an unmet postcondition.
    GUARDED = "guarded"

    #: Evidence was not examined, so no domain may be claimed.
    UNDETERMINED = "undetermined"


DOMAIN_REMEDIATION: dict[FaultDomain, str] = {
    FaultDomain.PERCEPTION: (
        "improve extraction: OCR engine or region selection, add UI Automation, or relax "
        "the verifier threshold. The action demonstrably took effect."),
    FaultDomain.DELIVERY: (
        "investigate the input path: key/event routing, foreground focus, target authority, "
        "event semantics, or the target's own key handling. Nothing reached the screen."),
    FaultDomain.APPLICATION_STATE: (
        "the action landed but the application diverged: inspect the evidence frame for a "
        "dialog, error, or unexpected state before changing input or perception."),
    FaultDomain.GUARDED: (
        "a safety guard refused the action; this is correct behaviour, not a defect. Read "
        "the refusal reason rather than the postcondition."),
    FaultDomain.UNDETERMINED: (
        "inspect the saved evidence frame and decide whether the asserted state is present "
        "or absent. Do not assign a fault domain before doing so."),
}


@dataclass(frozen=True, slots=True)
class FailureClass:
    domain: FaultDomain
    basis: str
    #: The artifact a human or agent must look at before trusting this classification.
    evidence_required: str = ""
    #: True when the classification rests on an inspected artifact rather than a default.
    evidence_backed: bool = False

    @property
    def remediation(self) -> str:
        return DOMAIN_REMEDIATION[self.domain]

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain.value,
            "basis": self.basis,
            "evidence_required": self.evidence_required,
            "evidence_backed": self.evidence_backed,
            "remediation": self.remediation,
        }

    def describe(self) -> str:
        tag = "evidence-backed" if self.evidence_backed else "NOT evidence-backed"
        return f"{self.domain.value} ({tag}): {self.basis}"


#: A text condition whose postcondition is only observable in pixels. A bare verdict on one
#: of these cannot support a domain claim, because the verdict carries no observation of
#: what was actually on screen.
_PIXEL_ONLY_CONDITIONS = frozenset({"text_matches", "text_absent", "region_matches"})


def classify_failure(
    condition: str,
    *,
    state_observed_present: bool | None = None,
    evidence_path: str = "",
    basis: str = "",
    guarded: bool = False,
) -> FailureClass:
    """Classify a failed verification, refusing to guess.

    ``state_observed_present`` is the answer to one question: *was the asserted state
    actually visible in the inspected evidence artifact?* ``None`` means nobody looked, and
    ``None`` is the only defensible default for a pixel-only condition.

    Passing ``True`` or ``False`` without naming the artifact inspected is refused, because
    an unbacked claim here is precisely the error this module exists to prevent.
    """
    if guarded:
        return FailureClass(
            FaultDomain.GUARDED,
            basis or "the run stopped on a safety guard, not on an unmet postcondition",
            evidence_backed=True,
        )

    pixel_only = condition in _PIXEL_ONLY_CONDITIONS

    if state_observed_present is None:
        return FailureClass(
            FaultDomain.UNDETERMINED,
            basis or (
                f"{condition!r} failed, but the evidence frame was not inspected, so it is "
                "not known whether the asserted state was on screen"),
            evidence_required=(
                "the saved evidence frame for this step: confirm whether the asserted "
                "state is present or absent before assigning a fault domain"),
        )

    # An inspection claim with nothing to point at is not an inspection claim.
    if not evidence_path:
        return FailureClass(
            FaultDomain.UNDETERMINED,
            basis=(
                f"reported that the state was "
                f"{'present' if state_observed_present else 'absent'} but named no "
                "evidence artifact, so the claim cannot be checked"),
            evidence_required="the path to the evidence frame that was inspected",
        )

    if state_observed_present:
        domain = FaultDomain.PERCEPTION
        default_basis = (
            f"the evidence frame {evidence_path} shows the asserted state, so the action "
            f"took effect and the {condition!r} verifier failed to read it")
    else:
        domain = FaultDomain.DELIVERY
        default_basis = (
            f"the evidence frame {evidence_path} does not show the asserted state, so the "
            f"action never took effect and {condition!r} is reporting that truthfully")

    return FailureClass(
        domain,
        basis=basis or default_basis,
        evidence_required=evidence_path,
        evidence_backed=True,
    )


def is_classifiable(condition: str) -> bool:
    """Whether this condition can support a domain claim at all without pixel evidence."""
    return condition not in _PIXEL_ONLY_CONDITIONS


__all__ = [
    "DOMAIN_REMEDIATION",
    "FailureClass",
    "FaultDomain",
    "classify_failure",
    "is_classifiable",
]
