"""The single enforced runtime path for live input.

Every live action - from the CLI, a runner, a scenario, a test helper, a scheduled job or a
future GUI - must traverse exactly this chain:

    ExecutionStateMachine  ->  TargetSession  ->  arm token  ->  TargetGuard
                           ->  Input Safety Controller  ->  approved backend

This module is that chain, assembled once. Nothing else is permitted to call the input
port. :func:`build_live_gate` returns the only object a caller may use to authorise an
action, and it fails closed: with no session, no arm token, or any failing check, the
result is a refusal and **zero input**.

The point of centralising it here is that the previous design allowed the same safety
components to exist, be unit-tested, and then not be used by the real runner at all.
A gate that is not on the path is not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frameforge.actions.arm import (
    ArmToken,
    BlockReason,
    BlockResult,
    ExecutionState,
    ExecutionStateMachine,
    GameplayVerdict,
)
from frameforge.actions.coordinates import ScreenPx
from frameforge.actions.target import (
    ProtectedRegistry,
    TargetGuard,
    TargetSession,
    ValidationResult,
    Verdict,
)
from frameforge.kernel.errors import FrameForgeError


class LiveInputDisabled(FrameForgeError):
    """Raised when something demands live input without a valid authorisation.

    A distinct type on purpose: a caller must be able to distinguish "the safety gate
    refused" from "the code is broken".
    """


@dataclass(slots=True)
class LiveGate:
    """The only authorised route to live input.

    Constructed once per run. ``authorize`` is the sole entry point; it returns a refusal
    rather than raising for an ordinary safety decision, so callers cannot accidentally
    treat a refusal as an error and retry.
    """

    machine: ExecutionStateMachine
    guard: TargetGuard
    policy: Any
    controller: Any
    registry: ProtectedRegistry | None = None
    #: Every decision, for the audit log.
    decisions: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------- authorisation

    def authorize(
        self,
        intent: str,
        *,
        point: ScreenPx | None = None,
        action_id: str = "",
        source: str = "",
    ) -> BlockResult:
        """Authorise one action. Any failure means zero input.

        Checked in this order, cheapest and most decisive first:

        1. the state machine is ACTIVE (the arm gate);
        2. the arm token is live and bound to this session;
        3. the runtime conditions - target identity, capture, foreground, gameplay;
        4. the point is inside an approved region and the window under it is the target;
        5. the point is not inside a protected window;
        6. the profile permits this intent.

        A refusal pauses the machine unless the machine is not yet ACTIVE, because pausing
        from OFF would be meaningless.
        """
        record: dict[str, Any] = {
            "action_id": action_id or f"a{len(self.decisions) + 1:05d}",
            "source": source,
            "intent": intent,
            "point": (point.x, point.y) if point else None,
        }

        # 1. Arm gate.
        if self.machine.state is not ExecutionState.ACTIVE:
            return self._refuse(
                BlockReason.NOT_ARMED,
                f"execution state is {self.machine.state.value}; live input is locked",
                record,
            )

        # 2. Arm token.
        token = self.machine.arm_token
        if token is None or not token.valid:
            return self._refuse(
                BlockReason.NO_ARM_TOKEN,
                token.voided_reason if token else "no user arm token is active",
                record,
            )

        # 3. Runtime conditions (identity, capture, foreground, gameplay, deadlines).
        runtime = self.machine.evaluate_runtime_conditions()
        if runtime.blocked:
            self.pause()
            return self._refuse(
                runtime.reason or BlockReason.NOT_ARMED, runtime.detail, record,
            )

        # 4 & 5. Point validation, when the action has one.
        if point is not None:
            verdict = self.guard.validate_point(point)
            record["guard"] = verdict.to_dict()
            if not verdict.ok:
                self.pause()
                return self._refuse(
                    self._map_verdict(verdict.verdict),
                    verdict.detail or "point failed target validation",
                    record,
                )

        # 6. Profile permission.
        profile = self.machine.profile
        if profile is not None:
            ok, why = profile.permits(intent)
            if not ok:
                return self._refuse(BlockReason.PROFILE_FORBIDS_ACTION, why, record)

        record["allowed"] = True
        self.decisions.append(record)
        return BlockResult.allow()

    def _refuse(self, reason: BlockReason, detail: str, record: dict[str, Any]) -> BlockResult:
        record.update({"allowed": False, "reason": reason.value, "detail": detail})
        self.decisions.append(record)
        return BlockResult.deny(reason, detail)

    @staticmethod
    def _map_verdict(verdict: Verdict) -> BlockReason:
        """Translate a guard verdict into a reportable block reason."""
        return {
            Verdict.NO_SESSION: BlockReason.SESSION_MISSING,
            Verdict.EXPIRED: BlockReason.SESSION_EXPIRED,
            Verdict.HWND_GONE: BlockReason.TARGET_HWND_INVALID,
            Verdict.PROCESS_CHANGED: BlockReason.TARGET_PROCESS_MISMATCH,
            Verdict.PROTECTED: BlockReason.PROTECTED_AT_POINT,
            Verdict.WRONG_WINDOW: BlockReason.POINT_NOT_IN_TARGET,
            Verdict.NOT_IN_REGION: BlockReason.POINT_OUTSIDE_REGION,
            Verdict.OUT_OF_BOUNDS: BlockReason.POINT_NOT_IN_TARGET,
            Verdict.TOPOLOGY_CHANGED: BlockReason.TOPOLOGY_CHANGED,
            Verdict.DPI_CHANGED: BlockReason.DPI_CHANGED,
            Verdict.MOVED: BlockReason.TARGET_HWND_INVALID,
            Verdict.SELF_FOREGROUND: BlockReason.SELF_FOREGROUND,
            Verdict.TARGET_NOT_FOREGROUND: BlockReason.TARGET_NOT_FOREGROUND,
        }.get(verdict, BlockReason.POINT_NOT_IN_TARGET)

    def pause(self) -> None:
        if self.machine.state is ExecutionState.ACTIVE:
            self.machine.pause_safe_stop("target validation failed")

    # -------------------------------------------------------------- dispatch

    def dispatch(self, intent: str, primitives: list[Any], **kw: Any) -> BlockResult:
        """Authorise and, only if authorised, hand primitives to the controller.

        The primitive list is built by the caller *before* authorisation, which is safe: a
        primitive is inert data. Nothing reaches the OS unless ``authorize`` allows.
        """
        result = self.authorize(intent, **kw)
        if result.blocked:
            return result
        outcome = self.controller.execute(
            _request_for(intent, kw.get("action_id", "")), primitives
        )
        if not outcome.ok:
            self.pause()
            return BlockResult.deny(BlockReason.PROFILE_FORBIDS_ACTION,
                                    outcome.detail or "controller refused")
        return result

    def status(self) -> dict[str, Any]:
        return {
            "state": self.machine.state.value,
            "input_locked": self.machine.state is not ExecutionState.ACTIVE,
            "banner": self.machine.banner(),
            "protected": self.registry.report() if self.registry else None,
            "decisions": self.decisions[-20:],
            "machine": self.machine.status(),
        }


def _request_for(intent: str, action_id: str):
    """Minimal ToolRequest for the controller, built from a validated intent."""
    from frameforge.actions.controller import ToolRequest

    return ToolRequest(action_id=action_id, source="live-gate")


def build_live_gate(
    *,
    machine: ExecutionStateMachine,
    session: TargetSession | None,
    policy: Any,
    controller: Any,
    registry: ProtectedRegistry | None = None,
    desktop: Any = None,
    topology_fingerprint: str = "",
) -> LiveGate:
    """Assemble the chain. With no session, the gate refuses everything - by construction."""
    if registry is None:
        registry = ProtectedRegistry()
        registry.protect_current_process()
    guard = TargetGuard(session=session, desktop=desktop, topology_fingerprint=topology_fingerprint)
    return LiveGate(machine=machine, guard=guard, policy=policy,
                    controller=controller, registry=registry)


__all__ = ["LiveGate", "LiveInputDisabled", "build_live_gate"]
