"""Exception hierarchy.

Design rule: every failure mode that the guardrails care about has a *distinct* type,
so callers can never accidentally treat "policy refused this" as "the network hiccuped".
Both fail closed, but they mean different things in a report.
"""

from __future__ import annotations


class FrameForgeError(Exception):
    """Base for all Frame Forge errors."""


# --------------------------------------------------------------------------- policy


class PolicyError(FrameForgeError):
    """A guardrail or authorisation rule refused the operation.

    Never recoverable by retry, by the planner, or by a flag. The only remedy is a
    change made by a human (guardrail G-LAW, G-AUTH-06).
    """


class AuthorizationError(PolicyError):
    """Profile lacks a valid ``authorized_use`` attestation."""


class BlastRadiusError(PolicyError):
    """An action's class requires consent the current scenario did not grant."""


# ------------------------------------------------------------------------ validation


class ValidationError(FrameForgeError):
    """A proposal failed the PlanValidator (guardrail G-VAL-*)."""


class SchemaValidationError(ValidationError):
    """Structurally invalid: bad types, unknown fields, missing required."""


class PermissionError_(ValidationError):
    """Structurally valid but the action is not permitted in this scope."""


class BudgetExceededError(FrameForgeError):
    """A ledger-enforced cap was reached. The run must degrade or stop, not continue."""


class HostilePlanError(ValidationError):
    """A plan attempted to widen its own permissions or carried injected instructions.

    Counted against the run's trust counter; three strikes disables the planner for
    the remainder of the run (G-VAL-08, G-INJ-03).
    """


# ------------------------------------------------------------------- runtime/state


class TargetLostError(FrameForgeError):
    """The target window could not be resolved, or its identity changed."""


class AmbiguousTargetError(TargetLostError):
    """A target spec matched several windows.

    Distinct from "not found" on purpose. Picking the first match would be a coin flip
    about which window Frame Forge is about to type into, and the resulting report would
    attribute the consequences to whichever window it happened to pick. Naming the
    candidates turns an inscrutable failure into a one-line profile fix.
    """

    def __init__(self, candidates: list[str] = ()) -> None:
        self.candidates = tuple(candidates)
        listed = "; ".join(candidates[:6]) or "(none)"
        more = f" (+{len(candidates) - 6} more)" if len(candidates) > 6 else ""
        msg = (
            f"target spec matched {len(candidates)} windows, refusing to guess: {listed}{more}. "
            "Narrow the profile's title_regex or class_name."
        )
        super().__init__(msg)


class FocusLostError(FrameForgeError):
    """Foreground identity drifted from the target. Input is blocked (G-SES-02)."""


class CaptureHealthError(FrameForgeError):
    """Capture is unusable: lost, black, frozen, or unsupported (e.g. fullscreen-excl.)."""


class SessionInactiveError(FrameForgeError):
    """The Windows console session is not interactive. No input may be sent (G-SES-01)."""


class HumanInputDetected(FrameForgeError):
    """Real human input was observed. Default policy is pause and hand back (G-SES-03)."""


class Estopped(FrameForgeError):
    """Emergency stop was triggered. Terminal for the run."""


class Aborted(FrameForgeError):
    """Run aborted deliberately (operator request, policy, or budget)."""


class TimeoutExceeded(FrameForgeError):
    """A step, phase or run wall-clock budget elapsed."""


class CapabilityUnavailable(FrameForgeError):
    """An optional capability (OCR backend, DXGI, AI provider) is not present.

    Never a crash. Adapters raise this so the director can degrade explicitly and the
    report can say *why* something is unavailable.
    """


class Redacted(Exception):
    """Internal signal used by the redaction filter. Never escapes the sink."""


__all__ = [
    "Aborted",
    "AuthorizationError",
    "BlastRadiusError",
    "BudgetExceededError",
    "CapabilityUnavailable",
    "CaptureHealthError",
    "Estopped",
    "FocusLostError",
    "FrameForgeError",
    "HumanInputDetected",
    "HostilePlanError",
    "PermissionError_",
    "PolicyError",
    "Redacted",
    "SchemaValidationError",
    "SessionInactiveError",
    "TargetLostError",
    "TimeoutExceeded",
    "ValidationError",
]