"""Plan validator - guardrail layer L2.

Every proposal, from any planner, passes through here before it can influence anything.
Nine checks, all mandatory, all pure (guardrail G-VAL-09: the same invalid response must
be rejected identically every time - no sampling, no randomness).

The validator operates on the *action*, never on who proposed it. An AI-proposed
clipboard write is rejected exactly like a human-proposed one (G-BLAST-01). That single
design choice is why provenance does not need to be trusted anywhere in the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from frameforge.actions.model import AI_AUTHORABLE, Action, BlastClass
from frameforge.kernel.errors import (
    BlastRadiusError,
    HostilePlanError,
    PermissionError_,
    SchemaValidationError,
    ValidationError,
)
from frameforge.ports.planner import PlanProposal, PlanRequest, PlanStep, PlannerTier

#: Patterns that indicate an attempt to override the envelope. Presence is *not*
#: proof of malice - a game window legitimately titled "Ignore Previous Objectives"
#: would match - so this raises HostilePlanError (which counts a strike) only in
#: combination with a permission-widening request, and otherwise is merely *recorded*
#: as suspicious text. See ``scan_injection``.
INJECTION_MARKERS: tuple[str, ...] = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)",
    r"you\s+are\s+now\s+(a|an|in)\s+",
    r"^\s*system\s*:",
    r"<\s*/?\s*(system|assistant)\s*>",
    r"reveal\s+your\s+(system\s+)?prompt",
    r"print\s+your\s+(system\s+)?prompt",
    r"bypass\s+(the\s+)?(guard|safety|filter|restriction)",
    r"developer\s+mode",
    r"override\s+(your\s+)?(restrictions|rules|policy|guardrails)",
    r"grant\s+(yourself|me)\s+",
)

#: Attempts to widen permissions. Always a strike regardless of how they are phrased,
#: because there is no legitimate reason for a game-playing agent to request them.
PERMISSION_WIDENING: tuple[str, ...] = (
    # Budget / limit escalation
    r"increase\s+(my\s+|the\s+|your\s+)?(budget|limit|cap|quota|allowance)",
    r"raise\s+(my\s+|the\s+|your\s+)?(budget|limit|cap|quota|allowance)",
    r"extend\s+(my\s+|the\s+)?(budget|limit|deadline|timeout)",
    r"no\s+need\s+to\s+(count|track)\s+(the\s+)?(budget|actions)",
    # Verification suppression
    r"skip\s+(the\s+)?(verification|check|guard|validation|assertion)",
    r"without\s+(verification|checking|validating|asking)",
    r"don'?t\s+(verify|check|validate)",
    r"no\s+need\s+to\s+verify",
    r"(treat|consider|regard|record)\s+(it|this|that|the\s+result)\s+as\s+(pass|passed|success)",
    r"mark\s+(it|this|the\s+result)\s+as\s+pass",
    r"assume\s+(passed|success|it\s+worked|everything\s+is\s+fine)",
    # Guard / safety disabling
    r"disable\s+(the\s+)?(guard|safety|estop|focus|check|budget|ledger)",
    r"bypass\s+(the\s+)?(guard|safety|filter|restriction|check|policy|validation)",
    r"turn\s+off\s+(the\s+)?(guard|safety|estop|focus|check)",
    r"override\s+(your\s+|the\s+)?(restrictions|rules|policy|guardrails|limits|safety)",
    # Self-elevation
    r"grant\s+(yourself|me|yourself\s+more|me\s+more)",
    r"give\s+(me|yourself)\s+(more\s+)?(permissions|authority|access|power)",
    r"widen\s+(my\s+|the\s+|your\s+)?(permissions|action\s+set|scope|authority)",
    r"escalate\s+(my\s+|your\s+)?(permissions|privileges|priority)",
    r"you\s+(now\s+)?have\s+(unlimited|full|elevated)\s+(budget|permissions|access)",
    r"(act|behave|operate)\s+as\s+(an?\s+)?(admin|root|developer|god)",
    r"developer\s+mode",
    # Reveal-the-envelope attempts
    r"(print|reveal|show|repeat)\s+your\s+(system\s+)?(prompt|instructions|rules)",
)

_PERMISSION_RE = tuple(re.compile(p, re.IGNORECASE) for p in PERMISSION_WIDENING)
_INJECTION_RE = tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in INJECTION_MARKERS)


@dataclass(slots=True)
class ValidationPolicy:
    """Limits and permissions for one validation call."""

    max_plan_steps: int = 5
    allowed_intents: frozenset[str] = frozenset()
    allowed_action_types: frozenset[str] = frozenset()
    ai_authored: bool = False
    allow_irreversible: bool = False
    require_expected_change: bool = True
    #: Reject the whole plan when one step is invalid, rather than dropping the step.
    #: Whole-plan rejection is the default: a partially-applied hostile plan is worse
    #: than a rejected one.
    atomic: bool = True


@dataclass(slots=True)
class ValidationOutcome:
    """Result of validating a proposal."""

    ok: bool
    steps: tuple[PlanStep, ...] = ()
    reason: str = ""
    rule_id: str | None = None
    hostile: bool = False
    suspicious_text: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.ok:
            return f"valid ({len(self.steps)} steps)"
        prefix = "HOSTILE" if self.hostile else "INVALID"
        rule = f" [{self.rule_id}]" if self.rule_id else ""
        return f"{prefix}{rule}: {self.reason}"


class PlanValidator:
    """Pure, deterministic validation of a plan proposal."""

    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()
        self.rejections = 0
        self.hostile_attempts = 0

    # --------------------------------------------------------------------- main

    def validate(self, proposal: PlanProposal, request: PlanRequest) -> ValidationOutcome:
        """Validate a proposal. Never raises for an invalid proposal - returns an outcome."""
        p = self.policy

        if proposal.abstain:
            # Abstaining is a legitimate, encouraged answer (guardrail G-DEC-05).
            return ValidationOutcome(ok=True, steps=(), reason=proposal.abstain_reason or "abstained")

        if len(proposal.steps) > p.max_plan_steps:
            self.rejections += 1
            return ValidationOutcome(
                ok=False,
                reason=f"plan has {len(proposal.steps)} steps, max {p.max_plan_steps}",
                rule_id="G-VAL-07",
            )

        # G-INJ-02/G-INJ-03: screen-derived text is data. Scan it, but only *strike* when
        # it also tries to widen permissions.
        suspicious = self.scan_injection(request)
        hostile_reason = self._hostile_check(proposal, request)

        if hostile_reason:
            self.hostile_attempts += 1
            self.rejections += 1
            return ValidationOutcome(
                ok=False, reason=hostile_reason, rule_id="G-INJ-03", hostile=True,
                suspicious_text=suspicious,
            )

        validated: list[PlanStep] = []
        for index, step in enumerate(proposal.steps):
            try:
                outcome = self._validate_step(step, request, index)
            except (AttributeError, TypeError) as exc:
                # A structurally malformed step is a rejection, not a crash. Fail closed
                # with a message rather than propagating a confusing AttributeError from
                # somewhere deep in the check.
                self.rejections += 1
                return ValidationOutcome(
                    ok=False,
                    reason=f"step {index} is malformed: {type(exc).__name__}: {exc}",
                    rule_id="G-VAL-01",
                    suspicious_text=suspicious,
                )
            if not outcome.ok:
                self.rejections += 1
                if p.atomic:
                    return ValidationOutcome(
                        ok=False, reason=outcome.reason, rule_id=outcome.rule_id,
                        hostile=outcome.hostile, suspicious_text=suspicious,
                    )
                continue
            validated.extend(outcome.steps)

        return ValidationOutcome(
            ok=True, steps=tuple(validated), suspicious_text=suspicious
        )

    # ---------------------------------------------------------------- per-step

    def _validate_step(
        self, step: PlanStep, request: PlanRequest, index: int
    ) -> ValidationOutcome:
        p = self.policy
        action = step.action

        # G-VAL-06: no postcondition means the "act and hope" pattern, which is
        # structurally unavailable. Wait/screenshot are exempt: their effect is timing
        # and evidence, both self-evident.
        if p.require_expected_change and not step.expected_change:
            if action.type not in ("wait", "screenshot"):
                return ValidationOutcome(
                    ok=False,
                    reason=f"step {index} ({action.type}) has no expected_change; "
                           "a postcondition is mandatory",
                    rule_id="G-VAL-06",
                )

        # G-VAL-02: allowlist membership. Unknown action types are rejected outright
        # rather than dropped.
        if p.allowed_action_types and action.type not in p.allowed_action_types:
            return ValidationOutcome(
                ok=False,
                reason=f"action type {action.type!r} not permitted in this scenario",
                rule_id="G-VAL-02",
            )

        if action.type == "intent":
            intent = getattr(action, "intent", "")
            if p.allowed_intents and intent not in p.allowed_intents:
                return ValidationOutcome(
                    ok=False,
                    reason=f"intent {intent!r} not permitted; allowed: "
                           f"{sorted(p.allowed_intents)[:12]}",
                    rule_id="G-VAL-02",
                )

        # G-VAL-05 / G-BLAST-01: blast radius, enforced on the action.
        blast = action.blast()
        try:
            self._check_blast(blast)
        except BlastRadiusError as exc:
            return ValidationOutcome(
                ok=False, reason=str(exc), rule_id="G-BLAST-01"
            )

        return ValidationOutcome(ok=True, steps=(step,))

    def _check_blast(self, blast: BlastClass) -> None:
        if blast is BlastClass.FORBIDDEN:
            msg = f"action class {blast} is forbidden unconditionally"
            raise BlastRadiusError(msg)
        if blast is BlastClass.EXTERNAL and not self.policy.allow_irreversible:
            msg = (
                "external-effect action requires allow_irreversible=true; "
                "see docs/AI_GUARDRAILS.md G-BLAST-02"
            )
            raise BlastRadiusError(msg)
        if self.policy.ai_authored and blast not in AI_AUTHORABLE:
            msg = f"AI planners may not author {blast} actions"
            raise BlastRadiusError(msg)

    # ---------------------------------------------------------------- injection

    @staticmethod
    def scan_injection(request: PlanRequest) -> tuple[str, ...]:
        """Record screen-derived text that looks like an instruction.

        Recorded, not acted upon. Screen text is untrusted data by construction; the
        point of recording it is auditability, so a report can show that the agent saw
        an injection attempt and correctly ignored it.
        """
        found: list[str] = []
        lines = [text for text, _rect, _conf in request.perception.ocr_lines]
        lines += [request.perception.window_title]
        for line in lines:
            if not line:
                continue
            for pattern in _INJECTION_RE:
                if pattern.search(line):
                    found.append(line[:120])
                    break
        return tuple(dict.fromkeys(found))

    def _hostile_check(self, proposal: PlanProposal, request: PlanRequest) -> str:
        """A permission-widening request is always a strike.

        Guardrail G-DEC-03: the planner cannot request a bigger budget, a longer timeout,
        a wider action set, or a skipped verification. There is no prompt or flag that
        grants this, and this method is the place that says so.
        """
        haystack = " ".join(
            [proposal.rationale] + [s.rationale for s in proposal.steps] + [s.expected_change or "" for s in proposal.steps]
        )
        for pattern in _PERMISSION_RE:
            if pattern.search(haystack):
                return f"planner attempted to widen permissions: matched {pattern.pattern!r}"
        return ""


__all__ = [
    "INJECTION_MARKERS",
    "PERMISSION_WIDENING",
    "PlanValidator",
    "ValidationOutcome",
    "ValidationPolicy",
]
