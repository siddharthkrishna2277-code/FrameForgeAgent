"""RunDirector - the deterministic state machine.

This is the system's spine and the only component permitted to advance state. Everything
else proposes, measures, or executes; the director decides what happens next and records
why.

The loop, verbatim:

    acquire target -> observe -> decide -> validate -> compile -> execute
                   -> verify -> (continue | recover | pause | stop)

Design properties that matter more than the sequence:

* **Nothing is trusted.** A planner proposes, the validator checks, the compiler resolves,
  the executor verifies the result, the verifier judges the outcome. A proposal that
  fails any stage does not reach the OS.
* **One write path to input.** Only :meth:`_execute` calls the executor, and only after
  every check has passed.
* **Every transition is an event.** The timeline in a report is the actual history, not a
  reconstruction.
* **The loop is sequential by design.** Concurrency in a state machine that reasons about
  a shared screen and a single cursor buys nothing and creates ordering hazards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from frameforge.actions.executor import ActionExecutor, ExecutionResult
from frameforge.actions.loopguard import LoopGuard, LoopSignal
from frameforge.actions.focus import FocusPolicy
from frameforge.actions.ledger import BudgetLedger
from frameforge.actions.model import Action, Intent, Screenshot, Wait
from frameforge.kernel.bus import EventLog
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import (
    Aborted,
    BudgetExceededError,
    CaptureHealthError,
    Estopped,
    FocusLostError,
    FrameForgeError,
    HumanInputDetected,
    SessionInactiveError,
    TargetLostError,
    TimeoutExceeded,
    ValidationError,
)
from frameforge.kernel.events import EventKind, PolicyVerdict
from frameforge.kernel.states import (
    PAUSED_STATES,
    RunState,
    assert_transition,
    can_transition,
)
from frameforge.perception.assembler import Observation
from frameforge.perception.health import HealthVerdict
from frameforge.perception.verify import Condition, ScreenChanged, Verdict, Verifier
from frameforge.ports.planner import (
    PerceptionSummary,
    PlanContext,
    PlanProposal,
    PlanRequest,
    PlannerTier,
)

if TYPE_CHECKING:  # pragma: no cover
    from frameforge.perception.assembler import FrameAssembler
    from frameforge.planning.validator import PlanValidator


@dataclass(slots=True)
class RunOutcome:
    """The result of a run, for the report and the CLI."""

    state: RunState
    steps_executed: int = 0
    steps_failed: int = 0
    steps_unknown: int = 0
    steps_skipped: int = 0
    verdicts: list[Verdict] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0
    planner_degraded: bool = False
    hostile_plans: int = 0

    @property
    def ok(self) -> bool:
        return self.state is RunState.COMPLETED


class RunDirector:
    """Owns the loop and all state transitions for one run."""

    def __init__(
        self,
        *,
        event_log: EventLog,
        assembler: FrameAssembler,
        verifier: Verifier,
        executor: ActionExecutor,
        validator: PlanValidator,
        ledger: BudgetLedger,
        policy_authorizer=None,
        clock: ClockPort | None = None,
        estop=None,
        planner=None,
    ) -> None:
        self.events = event_log
        self.assembler = assembler
        self.verifier = verifier
        self.executor = executor
        self.validator = validator
        self.ledger = ledger
        self.policy_authorizer = policy_authorizer
        self.clock = clock or SystemClock()
        self.estop = estop
        #: Optional PlannerPort. When set, the director asks it for the next action instead
        #: of using the step's declared action. This is the one pluggable slot in the loop.
        self.planner = planner
        #: Optional FocusGuard, attached so pause/resume can consult it.
        self._focus_guard = None

        self.state = RunState.IDLE
        self.outcome = RunOutcome(state=RunState.IDLE)
        self.transitions: list[tuple[RunState, RunState, float]] = []
        self.plan_trace: list[dict] = []
        #: Pre-execution intents, reconciled by ``_execute``. The audit trail must survive
        #: a crash *during* a batch, which post-hoc logging cannot.
        self._intents: list[dict] = []
        #: Advisory loop hygiene: repetition, stalls, per-action deadline.
        self.loop_guard = LoopGuard(clock=clock)
        self._t0 = 0.0

    # ---------------------------------------------------------------- transitions

    def transition(self, nxt: RunState, reason: str = "") -> None:
        """Advance state, recording it. Illegal transitions raise rather than warn."""
        if self.state is nxt:
            return
        assert_transition(self.state, nxt)
        previous = self.state
        self.state = nxt
        now = self.clock.monotonic_ms()
        self.transitions.append((previous, nxt, now))
        self.events.append(
            EventKind.RUN_STATE_CHANGED,
            "director",
            f"{previous} -> {nxt}" + (f" ({reason})" if reason else ""),
            {"from": str(previous), "to": str(nxt), "reason": reason},
        )

    def _event(self, kind: EventKind, summary: str, **data: object) -> None:
        """Record an event. Keyword arguments are folded into the payload by the sink."""
        self.events.append(kind, "director", summary, dict(data) if data else None)

    # -------------------------------------------------------------------- policy

    def authorize(self) -> PolicyVerdict:
        """Check authorisation before anything else happens.

        Refusal is terminal and there is no override path - not in the CLI, not in the
        API, not in a config file (guardrail G-AUTH-06).
        """
        if self.policy_authorizer is None:
            self._event(EventKind.POLICY_CHECKED, "no authorizer configured; treating as local test")
            return PolicyVerdict(allowed=True, reason="no authorizer (test mode)")
        verdict = self.policy_authorizer()
        kind = EventKind.POLICY_CHECKED if verdict.allowed else EventKind.POLICY_REFUSED
        self._event(kind, f"authorisation {'allowed' if verdict.allowed else 'refused'}: {verdict.reason}",
                    allowed=verdict.allowed, rule_id=verdict.rule_id, warnings=list(verdict.warnings))
        if not verdict.allowed:
            self._event(EventKind.POLICY_REFUSED, f"run refused: {verdict.reason}",
                        reason=verdict.reason, rule_id=verdict.rule_id)
            raise Aborted(f"policy refused run: {verdict.reason}")
        return verdict

    # ---------------------------------------------------------------------- loop

    def run(
        self,
        steps: list[dict],
        *,
        objective: str = "",
        fetch_observation=None,
        on_step_start=None,
    ) -> RunOutcome:
        """Execute ``steps`` - a list of ``{name, actions, condition, ...}`` dicts.

        The step shape is a plain dict rather than a task-DSL type so the director stays
        independent of the DSL; the runner adapts between them.
        """
        self._t0 = self.clock.monotonic_ms()
        self.transition(RunState.ARMING, "run start")
        self.outcome = RunOutcome(state=RunState.ARMING)
        self._event(EventKind.RUN_STARTED, f"run started: {objective}", objective=objective,
                    steps=len(steps))
        try:
            self.authorize()
            self._grace_period()
            self.transition(RunState.ACQUIRING_TARGET, "target acquisition")
            self.executor.arm()

            self.transition(RunState.OBSERVING)
            self._drain_sentinel()

            for index, step in enumerate(steps):
                self.outcome.steps_executed += 1
                self._drain_sentinel()
                self.ledger.check_runtime()
                # A previous step may have paused the run. Resume (or stay paused) before
                # deciding anything, rather than transitioning from a paused state.
                if self.state in PAUSED_STATES and not self.recover_from_pause(
                    reacquire=getattr(self, "reacquire", None)
                ):
                    self._finish(RunState.UNKNOWN, f"remained {self.state}; cannot continue")
                    return
                if on_step_start:
                    on_step_start(index, step)
                self._run_one_step(index, step, steps, fetch_observation)

            # A run that ended paused (focus lost, human input, session change) has not
            # completed its steps. Claiming COMPLETED here would be a false green, which is
            # the single worst thing a QA tool can do. Finish as UNKNOWN with the reason.
            if self.state in PAUSED_STATES:
                self._event(
                    EventKind.RUN_STATE_CHANGED,
                    f"run ended while {self.state}; reporting UNKNOWN rather than completed",
                    state=str(self.state),
                )
                self._finish(RunState.UNKNOWN, f"ended while {self.state}")
            else:
                self.transition(RunState.COMPLETED, "all steps processed")
                self.outcome.state = RunState.COMPLETED
        except Estopped as exc:
            self._event(EventKind.ESTOP_TRIGGERED, f"emergency stop: {exc}")
            self._finish(RunState.ABORTED, f"estopped: {exc}")
        except Aborted as exc:
            self._finish(RunState.ABORTED, f"aborted: {exc}")
        except BudgetExceededError as exc:
            self._event(EventKind.BUDGET_EXCEEDED, str(exc))
            self._finish(RunState.ABORTED, f"budget: {exc}")
        except TimeoutExceeded as exc:
            self._finish(RunState.FAILED, f"timeout: {exc}")
        except FrameForgeError as exc:
            self._finish(RunState.FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            # Release FIRST, then disarm. The reverse order means every key-up is sent
            # through a port that has already been disabled - and a disabled port drops
            # sends by design - so a key held at the moment of exit stays physically down.
            #
            # This is the exact shape of the release-blocking defect: a modifier left
            # logically pressed after the run, which the user's own diagnostic then read as
            # every keystroke being Ctrl+Shift+key.
            self.executor.release_everything()
            self.executor.disarm()

        self.outcome.duration_ms = self.clock.monotonic_ms() - self._t0
        self._event(
            EventKind.RUN_FINISHED,
            f"run finished: {self.state}",
            state=str(self.state),
            duration_ms=round(self.outcome.duration_ms, 1),
            steps=self.outcome.steps_executed,
            verdicts=len(self.outcome.verdicts),
            budget=self.ledger.snapshot(),
            intents=[i for i in self._intents if i.get("status") != "pending"],
            #: Any intent still 'pending' means the process stopped mid-batch. That is
            #: precisely the case post-hoc logging could not describe, and it is why the
            #: intent is written first.
            unresolved_intents=[
                {"intent_id": i["intent_id"], "step": i["step"], "actions": i["actions"]}
                for i in self._intents if i.get("status") == "pending"
            ],
            loop_guard=self.loop_guard.report(),
        )
        return self.outcome

    def _finish(self, state: RunState, reason: str) -> None:
        """Record a terminal state.

        Terminal states are absorbing, so the transition table would reject re-entering
        them. They are recorded directly rather than through ``transition`` for that
        reason, but the *reason* is always recorded alongside - a run that ends in an
        unexplained FAILED is worse than one that crashed.
        """
        self.outcome.state = state
        self.outcome.error = reason
        previous = self.state
        self.state = state
        self.transitions.append((previous, state, self.clock.monotonic_ms()))
        self.events.append(
            EventKind.RUN_FINISHED,
            "director",
            f"run {state}: {reason}",
            {"reason": reason, "from": str(previous), "to": str(state)},
        )

    def _grace_period(self) -> None:
        """Mandatory countdown before the first input.

        The user must be able to abort a run before it touches anything
        (guardrail G-SES-04). Length is a parameter so tests can shorten it; production
        uses 5 seconds.
        """
        seconds = getattr(self, "grace_seconds", 0.0)
        if seconds <= 0:
            return
        self._event(EventKind.RUN_STATE_CHANGED, f"grace period: {seconds:.0f}s before any input",
                    grace_seconds=seconds)
        self.clock.sleep_ms(seconds * 1000.0)

    def _drain_sentinel(self) -> None:
        """Check the ABORT sentinel file. Estop path 2."""
        if self.estop is not None and self.estop.poll_sentinel():
            raise Estopped("abort sentinel file present")

    # ---------------------------------------------------------------- one step

    def _run_one_step(
        self,
        index: int,
        step: dict,
        all_steps: list[dict],
        fetch_observation,
    ) -> None:
        name = step.get("name", f"step_{index}")
        attempts = int(step.get("attempts", 1))
        before = fetch_observation() if fetch_observation else None

        # Steps may declare actions/conditions as values or as zero-argument callables.
        # Lazy resolution matters: a click on a landmark can only be computed from the
        # observation of *this* epoch, so authored coordinates would otherwise have to be
        # guessed at plan time.
        declared = _resolve(step.get("actions"))

        if self.planner is not None:
            # A planner, when configured, owns the decision. Its proposal is validated
            # below exactly like any other source - being the planner grants no exemption.
            proposal = self._consult_planner(name, step, index, all_steps, fetch_observation)
            if proposal is None:
                return
            if proposal.abstain:
                verdict = Verdict(
                    disposition="unknown", condition="planner_abstained",
                    detail=proposal.abstain_reason or "planner abstained", confidence=0.0,
                )
                self._record_verdict(verdict, name)
                self.transition(RunState.OBSERVING)
                return
            actions = [s.action for s in proposal.steps]
            self._event(EventKind.PLAN_PROPOSED,
                        f"{name}: {len(actions)} action(s) from {proposal.tier.value}",
                        step=name, tier=str(proposal.tier), planner=proposal.planner_id,
                        rationale=proposal.rationale)
            if proposal.degraded:
                self._event(EventKind.PLANNER_DEGRADED,
                            f"{name}: planner degraded ({proposal.abstain_reason or 'unspecified'})",
                            step=name, detail=proposal.abstain_reason)
        else:
            actions = declared

        condition = _resolve(step.get("condition")) or ScreenChanged()

        if isinstance(actions, list) and not actions:
            # A step with no input: either a plain assertion, or a *wait* for a condition.
            #
            # These need different treatment. A plain assertion is evaluated once. A wait
            # must poll until its timeout, because "wait for the loading screen to finish"
            # has no other correct implementation - evaluating it once is exactly the
            # sleep-and-assume pattern this system exists to avoid.
            poll = bool(step.get("poll_until_true", False)) or attempts > 1
            timeout_ms = int(step.get("timeout_ms", 10_000) or 10_000)
            self.transition(RunState.VERIFYING, f"{name}: {'wait' if poll else 'verify-only'}")
            if poll and fetch_observation is not None:
                verdict = self.verifier.wait_for(
                    condition, before, fetch_observation,
                    timeout_ms=timeout_ms, poll_ms=max(60, int(step.get("settle_ms", 250))),
                    clock=self.clock,
                )
            else:
                verdict = self.verifier.evaluate(condition, before, fetch_observation())
            self._record_verdict(verdict, name)
            if verdict.ok:
                self.ledger.reset_retries()
            self.transition(RunState.OBSERVING)
            return

        settle_ms = int(step.get("settle_ms", 0) or 0)
        self.loop_guard.begin_step()

        for attempt in range(max(1, attempts)):
            step["attempt_index"] = attempt
            self.transition(RunState.DECIDING, f"{name} attempt {attempt + 1}")
            try:
                self._check_confidence_gate(fetch_observation)
                self._validate_actions(actions, ai_authored=bool(step.get("ai_authored")))
            except ValidationError as exc:
                self._event(EventKind.PLAN_REJECTED, f"{name}: {exc}", step=name, reason=str(exc))
                verdict = Verdict(disposition="unknown", condition=condition.name,
                                  detail=f"plan rejected: {exc}", confidence=0.0)
                self._record_verdict(verdict, name)
                self.transition(RunState.OBSERVING)
                return

            self.transition(RunState.EXECUTING, name)
            # Re-grab the baseline immediately before sending input. The observation taken
            # at step start may be many milliseconds - and on a slow capture backend,
            # several hundred - stale by now, and a stale baseline makes a change
            # detection silently meaningless.
            before = fetch_observation() if fetch_observation else None

            # Record the intent *before* anything is sent.
            #
            # Every action was previously appended to the trace only after execution
            # completed. If the process died mid-batch - the estop case, a crash, a killed
            # terminal - the one thing the operator needs afterwards is exactly what was
            # attempted, and that record did not exist. Pre-logging makes the audit trail
            # complete precisely when it matters most.
            intent_id = self._log_intent(name, step, actions)

            try:
                result = self._execute(actions, step, intent_id=intent_id)
            except (FocusLostError, HumanInputDetected, SessionInactiveError) as exc:
                self._handle_interruption(exc, name)
                return

            # Let the UI react before verifying. A game at 25 fps needs ~40ms to repaint;
            # comparing frames across a zero-delay boundary measures the compositor, not the
            # application. This is a bounded wait tied to a postcondition, not a blind sleep.
            if settle_ms > 0:
                self.clock.sleep_ms(settle_ms)

            self.transition(RunState.VERIFYING, f"{name}: postcondition")
            after = fetch_observation() if fetch_observation else None
            if after is None:
                verdict = Verdict(disposition="unknown", condition=condition.name,
                                  detail="no observation after execution")
            else:
                verdict = self.verifier.evaluate(condition, before, after)
            self._record_verdict(verdict, name)

            if step.get("is_setup") and not verdict.ok:
                # Setup is best-effort by design: a scenario may legitimately already be in
                # the desired state, so "dismiss the menu" failing is not a failure.
                self._event(
                    EventKind.RECOVERY_ATTEMPT,
                    f"{name}: setup step did not take effect, continuing",
                    step=name, detail=verdict.detail,
                )
                self.transition(RunState.OBSERVING)
                return

            if verdict.ok:
                self.ledger.reset_retries()
                self.loop_guard.note_progress()
                self.transition(RunState.OBSERVING)
                return
            if verdict.unknown:
                self.ledger.charge_unknown()
                self._event(EventKind.VERIFIER_UNKNOWN, f"{name}: UNKNOWN - {verdict.detail}",
                            step=name, detail=verdict.detail)

            if attempt < attempts - 1:
                self.ledger.charge_retry()
                backoff = min(2000.0, 200.0 * (2**attempt))
                self._event(EventKind.RECOVERY_ATTEMPT, f"{name}: retry in {backoff:.0f}ms",
                            step=name, attempt=attempt + 1)
                self.clock.sleep_ms(backoff)
                before = fetch_observation() if fetch_observation else before
                continue

        self.transition(RunState.OBSERVING)

    def _consult_planner(self, name, step, index, all_steps, fetch_observation):
        """Ask the configured planner for this step's actions.

        The observation is fetched here so the planner sees *this* epoch's state, and the
        same observation is then reused as the verification baseline - one capture, not two.
        """
        from frameforge.ports.planner import PlanContext, PlanRequest

        observation = fetch_observation() if fetch_observation else None
        summary = (
            observation.to_summary()
            if observation is not None
            else PerceptionSummary(mono_ms=0.0, frame_index=-1, frame_hash="",
                                   surface_size=(0, 0))
        )
        step_obj = step.get("task_step")
        context = PlanContext(
            objective=step.get("objective", "") or name,
            allowed_intents=tuple(self.validator.policy.allowed_intents),
            current_step=name,
            step_index=index,
            total_steps=len(all_steps),
            # Verdict has no step name; the step's own condition is the useful signal.
            recent_failures=tuple(
                f"{v.condition}:{v.disposition}" for v in self.outcome.verdicts[-3:]
                if v.disposition != "pass"
            ),
            recent_transitions=tuple(
                f"{frm}->{to}" for frm, to, _t in self.transitions[-4:]
            ),
            budget_actions_left=self.ledger.actions_left or 0,
            budget_ai_calls_left=self.ledger.ai_calls_left or 0,
            attempts_at_step=int(step.get("attempt_index", 0)),
            recovery_mode=bool(step.get("recovery_mode", False)),
        )
        request = PlanRequest(context=context, perception=summary)
        try:
            return self.planner.propose(request)
        except Exception as exc:
            # A planner that raises must not end the run; fall back to the declared action.
            self._event(EventKind.PLANNER_DEGRADED,
                        f"planner raised {type(exc).__name__}: {exc}; using declared action",
                        step=name, error=str(exc))
            return None

    # ------------------------------------------------------------- inner stages

    def _check_confidence_gate(self, fetch_observation) -> None:
        """Refuse to act on low confidence; probes only (guardrail G-DEC-01).

        Implemented as a rejection rather than a silent downgrade: the plan validator's
        job is to refuse, and inventing a "safe alternative" here would put a second,
        untested decision path in the safety-critical path.
        """
        if fetch_observation is None:
            return
        observation = fetch_observation()
        if observation is None:
            return
        threshold = getattr(self, "min_confidence_to_act", 0.20)
        if observation.confidence < threshold:
            msg = (
                f"confidence {observation.confidence:.2f} below action threshold {threshold:.2f}; "
                "probing or halting"
            )
            raise ValidationError(msg)

    def _validate_actions(self, actions: list[Action], *, ai_authored: bool) -> None:
        """Run each action through the validator. No action bypasses this."""
        from frameforge.ports.planner import PlanProposal, PlanStep

        proposal = PlanProposal(
            steps=tuple(PlanStep(action=a, expected_change="validated by director")
                        for a in actions),
            tier=PlannerTier.TIER1_AI if ai_authored else PlannerTier.TIER0_PROFILE,
        )
        request = PlanRequest(context=PlanContext(objective="director step"),
                              perception=self._current_summary)
        self.validator.policy.ai_authored = ai_authored
        outcome = self.validator.validate(proposal, request)
        if outcome.hostile:
            self.outcome.hostile_plans += 1
            self._event(EventKind.HOSTILE_PLAN, f"hostile plan blocked: {outcome.reason}",
                        reason=outcome.reason, suspicious=list(outcome.suspicious_text))
        if outcome.suspicious_text:
            self._event(EventKind.INJECTION_SUSPECTED,
                        f"injection-like text seen on screen: {list(outcome.suspicious_text)[:3]}",
                        lines=list(outcome.suspicious_text))
        if not outcome.ok:
            raise ValidationError(outcome.describe())

    def _log_intent(self, name: str, step: dict, actions: list[Action]) -> str:
        """Append a pre-execution intent record and return its id."""
        intent_id = f"{name}#{len(self.plan_trace) + 1:04d}"
        self._intents.append({
            "intent_id": intent_id,
            "step": name,
            "mono_ms": round(self.clock.monotonic_ms(), 2),
            "actions": [a.describe() for a in actions],
            "action_records": [_safe_dump(a) for a in actions],
            "status": "pending",
        })
        self._event(
            EventKind.PLAN_PROPOSED,
            f"{name}: intent recorded for {len(actions)} action(s) (pre-execution)",
            step=name, intent_id=intent_id, actions=[a.describe() for a in actions],
        )
        return intent_id

    def _execute(self, actions: list[Action], step: dict,
                 *, intent_id: str = "") -> ExecutionResult:
        """The single write path to input. Compiles, then executes."""
        compiler = step["compiler"]
        total = 0
        intent_record = next(
            (i for i in reversed(self._intents) if i["intent_id"] == intent_id), None
        )
        for action in actions:
            if isinstance(action, Wait):
                self.clock.sleep_ms(action.ms)
                continue
            if isinstance(action, Screenshot):
                self._event(EventKind.CAPTURE_FRAME, f"explicit screenshot {action.tag}", tag=action.tag)
                continue
            compiled = compiler.compile(action)
            self.loop_guard.observe(action)
            result = self.executor.execute(compiled.primitives,
                                            allow_hold=compiled.expects_hold)
            total += result.primitives_sent
            # The full action payload, not just a description: this is what
            # ``frameforge replay`` rebuilds the plan from. A description string cannot be
            # round-tripped, and a plan that cannot be replayed is not a deterministic
            # record.
            try:
                action_record = action.model_dump(mode="json")
            except Exception:
                action_record = {"type": action.type}
            self.plan_trace.append({
                "step": step.get("name", ""),
                "action": action.describe(),
                "action_record": action_record,
                "compiled": compiled.describe(),
                "blast": str(compiled.blast),
                "primitives": [p.describe() for p in compiled.primitives],
                # Two distinct facts. "dispatched" is how many primitives the executor
                # forwarded; "delivered_to_os" is how many the port confirmed. In
                # --dry-run the first is non-zero and the second is zero, and a reader
                # must be able to tell that apart at a glance.
                "dispatched": result.primitives_sent,
                "delivered_to_os": result.delivered_to_os,
                "blocked": result.blocked_reason,
                "duration_ms": round(result.duration_ms, 2),
                "mono_ms": round(self.clock.monotonic_ms(), 2),
                # Orthogonal outcomes, reported independently.
                "outcome": result.outcome,
                "intent_id": intent_id,
                "denied": result.denied,
                "partial": result.partial,
                "error": result.error,
            })
            if intent_record is not None:
                intent_record["status"] = result.outcome
                intent_record["dispatched"] = result.primitives_sent
                intent_record["delivered_to_os"] = result.delivered_to_os
                intent_record["reconciled_mono_ms"] = round(self.clock.monotonic_ms(), 2)
            self._event(
                EventKind.ACTION_EXECUTED,
                compiled.describe(),
                step=step.get("name", ""), action=action.describe(),
                blast=str(compiled.blast),
                # Both facts, always. "sent" alone read as "reached the operator's desktop",
                # which is false for every dry run and every disarmed port.
                dispatched=result.primitives_sent,
                delivered_to_os=result.delivered_to_os,
                blocked=result.blocked_reason,
            )
            if result.blocked_reason:
                self._event(EventKind.ACTION_BLOCKED, f"blocked: {result.blocked_reason}",
                            reason=result.blocked_reason)
                raise _blocked_error(result.blocked_reason)
        return ExecutionResult(total, self.clock.monotonic_ms() - self._t0)

    def recover_from_pause(self, reacquire=None) -> bool:
        """Attempt to resume after a pause. Returns True if the run can continue.

        Pause without resume is not a pause, it is a dead end: the observed behaviour was
        that the loop detected focus loss, recorded it, and then carried on from a paused
        state into an illegal transition. The intended cycle is

            paused -> re-acquire target -> re-foreground -> re-verify -> observing

        Refocusing only happens under an explicit ``refocus`` policy. Under the default
        ``pause`` policy the run resumes *without* stealing focus, which means it will only
        proceed if the operator has already brought the target back - the conservative
        behaviour, and the reason the default is not ``refocus``.
        """
        if self.state not in PAUSED_STATES:
            return True

        previous = self.state
        self.transition(RunState.ACQUIRING_TARGET, f"resuming from {previous}")

        if reacquire is not None:
            try:
                reacquire()
            except Exception as exc:
                self._event(EventKind.WINDOW_LOST, f"re-acquisition during resume failed: {exc}", error=str(exc))

        focus = getattr(self, "_focus_guard", None)
        refocused = False
        if focus is not None:
            if focus.policy is FocusPolicy.REFOCUS:
                refocused = focus.try_refocus()
                self._event(
                    EventKind.FOCUS_RESTORED if refocused else EventKind.FOCUS_LOST,
                    "refocus " + ("succeeded" if refocused else "failed"),
                    policy="refocus",
                )
            # Under `pause`, do not steal focus. Verify instead: if the operator has
            # already brought the target back, the run may continue.
            try:
                focus.assert_focus()
            except FrameForgeError as exc:
                self._event(EventKind.FOCUS_LOST, f"cannot resume yet: {exc}", error=str(exc))
                self.state = previous  # stay paused; the next step will retry
                return False

        self.transition(RunState.OBSERVING, f"resumed from {previous}")
        return True

    def _handle_interruption(self, exc: Exception, name: str) -> None:
        """Focus loss / human input / inactive session.

        None of these are failures of the run. They are the system refusing to act on a
        shared desktop, which is the whole point of the guards. The run pauses and the
        step is marked UNKNOWN so the report says *why* rather than claiming success.
        """
        match exc:
            case FocusLostError():
                self._event(EventKind.FOCUS_LOST, f"{name}: {exc}", step=name)
                target = RunState.PAUSED_FOCUS
            case HumanInputDetected():
                self._event(EventKind.HUMAN_INPUT_DETECTED, f"{name}: {exc}", step=name)
                target = RunState.PAUSED_USER
            case SessionInactiveError():
                self._event(EventKind.SESSION_INACTIVE, f"{name}: {exc}", step=name)
                target = RunState.PAUSED_HEALTH
            case _:
                target = RunState.FAILED
        verdict = Verdict(disposition="unknown", condition="interrupted",
                          detail=f"{type(exc).__name__}: {exc}", confidence=0.0)
        self._record_verdict(verdict, name)
        if can_transition(self.state, target):
            self.state = target
            self.transitions.append((self.state, target, self.clock.monotonic_ms()))

    # ------------------------------------------------------------------ verdicts

    def _record_verdict(self, verdict: Verdict, step_name: str) -> None:
        self.outcome.verdicts.append(verdict)
        kind = {
            "pass": EventKind.VERIFIER_EVALUATED,
            "fail": EventKind.VERIFIER_EVALUATED,
            "unknown": EventKind.VERIFIER_UNKNOWN,
        }[str(verdict.disposition)]
        self._event(
            kind,
            f"{step_name}: {verdict.describe()}",
            step=step_name,
            disposition=str(verdict.disposition),
            condition=verdict.condition,
            detail=verdict.detail,
            expected=verdict.expected,
            actual=verdict.actual,
            confidence=round(verdict.confidence, 4),
            frame_hash=verdict.frame_hash,
            frame_index=verdict.frame_index,
        )

    # ------------------------------------------------------------- observation

    _current_summary = None

    def set_observation(self, observation: Observation) -> None:
        """Record the latest observation so the validator can scan it for injection."""
        self._current_summary = observation.to_summary()


def _safe_dump(action) -> dict:
    """Serialise an action for the intent record, never raising.

    An audit record that can itself throw is worse than a slightly less detailed one: it
    would fail on the one path that exists precisely because something already went wrong.
    """
    try:
        return action.model_dump(mode="json")
    except Exception:
        return {"type": getattr(action, "type", "unknown"),
                "describe": str(getattr(action, "describe", lambda: action)())}


def _resolve(value):
    """Resolve a possibly-lazy step field."""
    return value() if callable(value) else value


def _blocked_error(reason: str) -> FrameForgeError:
    """Turn a blocked batch into the right exception type.

    Preserving the specific type matters: a focus block must pause, a human-input block
    must hand back, and a budget block must abort. Collapsing them into one error would
    destroy exactly the information the report needs.
    """
    lowered = reason.lower()
    if "focus" in lowered:
        return FocusLostError(reason)
    if "human input" in lowered:
        return HumanInputDetected(reason)
    if "session" in lowered:
        return SessionInactiveError(reason)
    if "estopped" in lowered:
        return Estopped(reason)
    if "budget" in lowered:
        return BudgetExceededError(reason)
    return CaptureHealthError(reason)


__all__ = ["RunDirector", "RunOutcome"]
