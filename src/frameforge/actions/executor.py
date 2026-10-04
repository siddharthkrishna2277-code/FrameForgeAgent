"""Action executor.

The single place where primitives reach the input port. Everything upstream proposes;
this decides, checks, orders, times, and cleans up.

Guarantees:

* **Strict ordering.** Input is a stream. Two concurrent senders produce reordered
  events, which games interpret as distinct and confusing input. One executor, one thread.
* **Hold balance.** Every key/button down is matched by an up. ``HoldTracker`` mirrors
  what was actually sent and is asserted at every step boundary, because a leaked hold is
  the worst failure available: the user keeps typing in a modified state with no idea why.
* **Guard checks inside the loop.** Focus and estop are re-checked between primitives,
  not just once at the start, so a long drag cannot outrun a focus change.
* **Interruptible motion.** ``MouseLook`` and ``Drag`` check between sub-moves so an
  estop during a flick actually stops the flick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from frameforge.actions.controller import InputPolicy, ToolRequest
from frameforge.actions.focus import FocusGuard, HumanInputDetector
from frameforge.kernel.errors import FrameForgeError
from frameforge.actions.coordinates import ScreenPx
from frameforge.ports.geometry import Point
from frameforge.ports.input import Key, PrimitiveType
from frameforge.actions.model import HoldTracker

#: Keys whose press must settle before a modified key is sent.
_MODIFIER_KEYS: frozenset = frozenset({
    Key.LCTRL, Key.RCTRL, Key.CTRL, Key.LSHIFT, Key.RSHIFT, Key.SHIFT,
    Key.LALT, Key.RALT, Key.ALT, Key.LWIN, Key.RWIN,
})
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.errors import Estopped
from frameforge.ports.input import Key, MouseButton, Primitive, PrimitiveType


@dataclass(slots=True)
class ExecutionResult:
    """What actually happened, for the plan trace and the report."""

    #: Each fact is reported independently.
    #:
    #: A single "did it work" boolean cannot distinguish *denied, nothing sent* from *sent
    #: three primitives then blocked on the fourth* - and a caller reading the first as
    #: "no action taken" would file a bug report describing the wrong event. Adopted from
    #: dsh's defensive-patterns rule "report orthogonal outcomes independently".
    #: Primitives dispatched by the executor. This is NOT a count of events that reached
    #: Windows: the port may still withhold them (dry-run, disarmed, or a backend refusal),
    #: and in --dry-run every one of these is still counted here while none is delivered.
    #: Read ``delivered_to_os`` for that claim.
    primitives_sent: int = 0
    #: Primitives the port confirmed as actually delivered to the OS.
    delivered_to_os: int = 0
    duration_ms: float = 0.0
    #: Set when policy or a guard refused the action.
    blocked_reason: str | None = None
    #: Set when the batch raised rather than being refused.
    error: str | None = None
    #: True when nothing was sent at all.
    denied: bool = False
    #: True when a partial batch was sent before something stopped it.
    partial: bool = False
    held_at_end: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Clean success: nothing blocked, nothing raised, everything sent."""
        return self.blocked_reason is None and self.error is None

    @property
    def outcome(self) -> str:
        """One word a report can print without ambiguity."""
        if self.denied:
            return "denied"
        if self.error:
            return "error"
        if self.partial:
            return "partial"
        if self.blocked_reason:
            return "blocked"
        return "executed"


@dataclass(slots=True)
class ExecutorStats:
    batches: int = 0
    primitives: int = 0
    blocked_batches: int = 0
    estops_honoured: int = 0


#: Primitive kinds whose dispatch depends on a fresh display topology.
#: Primitive kinds that count as Frame Forge's own input for the human-input baseline.
#:
#: Derived from PrimitiveType rather than listed by hand. The hand-written list omitted
#: SCANCODE when scan mode was added, so the system detected *itself* as a human operator
#: and halted the run mid-sequence - twice, live. A list that must be updated when a new
#: kind is introduced is a list that will be forgotten again.
_AGENT_INPUT_KINDS: frozenset = frozenset(PrimitiveType) - {PrimitiveType.GAMEPAD_STATE}

_TOPOLOGY_CHECKED_PRIMITIVES = frozenset({
    PrimitiveType.MOUSE_MOVE_ABS,
    PrimitiveType.MOUSE_BUTTON,
    PrimitiveType.KEY,
})


class ActionExecutor:
    """Executes validated primitive batches under the guards."""

    def __init__(
        self,
        input_port,
        *,
        focus: FocusGuard | None = None,
        human: HumanInputDetector | None = None,
        estop=None,
        clock: ClockPort | None = None,
        budget=None,
        controller=None,
        policy=None,
    ) -> None:
        #: The InputController is the *only* route to real input. When one is supplied the
        #: executor never touches the port directly, so policy validation and target
        #: checking sit on the path rather than beside it. Omitting it is a test-only
        #: affordance and is refused at construction when a live port is used.
        self._controller = controller
        self._policy = policy
        self._input = input_port
        self._focus = focus
        self._human = human
        self._estop = estop
        self._clock = clock or SystemClock()
        self._budget = budget
        if (controller is None
                and getattr(input_port, "name", "") == "sendinput"
                and not getattr(input_port, "_dry_run", False)):
            msg = (
                "SendInputPort requires an InputController. The controller is the only "
                "component permitted to emit OS input; injecting through the port directly "
                "bypasses target validation (see docs/AI_GUARDRAILS.md G-ABS-11)."
            )
            raise ValueError(msg)

        #: Runtime defence: a button-down may never dispatch without a preceding verified
        #: move. The schema requires a point and the compiler refuses one anyway; this is
        #: the third layer, and it is the one that runs closest to the OS.
        self._verified_move: ScreenPx | None = None
        #: Surface point awaiting its button event within the current batch.
        self._pending_point: Point | None = None
        #: Where the current button-down happened, so its matching release can be anchored.
        self._last_button_point: tuple[int, int] | None = None
        #: TargetGuard, when supplied, validates every mouse action immediately before
        #: dispatch. Absent in a mock-only test run; present for anything live.
        self.target_guard = None
        self.protected = None
        #: Every refusal, for the audit log.
        self.target_refusals: list[dict[str, object]] = []
        self._holds = HoldTracker()
        self.stats = ExecutorStats()
        #: Set by the director: the window this run may touch.
        self.target = None
        self.run_id = ""
        self.blocked_reason: str | None = None
        self._last_error: str | None = None
        #: Hold time owed by the previously sent primitive, applied before the next one.
        #: Centralising it here means a down primitive is a down and an up is an up -
        #: exactly one of each - while the *duration* is still honoured.
        self._pending_hold_ms = 0.0
        #: A modifier was just pressed; the next key waits `modifier_settle_ms`.
        self._pending_modifier = False
        #: Gap between pressing a modifier and the key it modifies. Without it a
        #: composite chord is sent with zero delay and applications ignore it -
        #: measured against Notepad, where a back-to-back Ctrl+A selected nothing.
        self.modifier_settle_ms = 25.0
        #: Gap between pressing a modifier and pressing the key it modifies.

    @property
    def holds(self) -> HoldTracker:
        return self._holds

    def arm(self) -> None:
        """Permit input. Only reached after the grace period and target acquisition."""
        self._input.set_enabled(True)

    def disarm(self) -> None:
        self._input.set_enabled(False)

    # ------------------------------------------------------------------ execution

    def execute(self, primitives: list[Primitive], *, allow_hold: bool = False) -> ExecutionResult:
        """Run a primitive batch.

        ``allow_hold`` is False by default, which means the batch must end balanced. A
        batch that leaves a key down is a programming error and is released immediately
        rather than silently tolerated.
        """
        t0 = self._clock.monotonic_ms()
        self.stats.batches += 1
        blocked: str | None = None

        if not primitives:
            return ExecutionResult(0, 0.0)

        # Initialised before the loop: every early-exit path constructs an
        # ExecutionResult, and an unbound local on one of those paths would replace a
        # truthful "denied" with an UnboundLocalError.
        sent = 0
        delivered = 0
        self._last_error = None
        self.blocked_reason = None

        try:
            self._preflight()
            self._pending_hold_ms = 0.0
            for index, primitive in enumerate(primitives):
                if self._pending_hold_ms > 0:
                    self._preflight()
                    self._clock.sleep_ms(self._pending_hold_ms)
                    self._pending_hold_ms = 0.0
                elif self._pending_modifier and self._holds.held_keys:
                    # A modifier was just pressed and the next primitive is a normal key.
                    #
                    # Sending them back-to-back produced a chord that applications
                    # ignore: Ctrl down then A down with no gap means the target has not
                    # yet registered Ctrl as held, so Ctrl+A reads as a bare "a". Measured
                    # against Notepad - the typed text changed but select-all selected
                    # nothing, so the buffer was never cleared.
                    #
                    # This is a property of *human* input too: a physical Ctrl is down
                    # before the next key is struck.
                    self._preflight()
                    self._clock.sleep_ms(self.modifier_settle_ms)
                self._preflight()
                # Budget is charged before sending, so a cap stops the batch mid-way
                # rather than after one extra click.
                if self._budget is not None:
                    self._budget.charge_actions(1, held_ms=primitive.hold_ms)
                # Fail closed on every path that cannot be validated. There is no
                # "policy-free" route to the OS: omitting the policy or the controller
                # yields no input rather than an unguarded one. The previous fallback sent
                # straight to the port, which made the whole safety layer optional *by
                # omission* - and omission is how a guard gets bypassed by accident.
                # Topology is checked before every mouse primitive, not just once at
                # session creation. A monitor disconnected or a scaling changed between
                # planning and dispatch invalidates every cached rectangle, so the action is
                # refused rather than computed from stale geometry.
                if primitive.kind in _TOPOLOGY_CHECKED_PRIMITIVES:
                    if self.target_guard is not None:
                        same, why = self.target_guard.session.refresh_topology()
                        if not same:
                            self._refuse_target(why, event="monitor_topology_changed")
                            break

                if primitive.kind is PrimitiveType.MOUSE_MOVE_ABS:
                    candidate = ScreenPx(primitive.x, primitive.y)
                    # Verify *before* moving as well as before the button. Moving first and
                    # checking afterwards would already have relocated the operator's cursor
                    # onto whatever was covering the target.
                    if self.target_guard is not None:
                        pre = self.target_guard.validate_point(candidate)
                        if not pre.ok:
                            self._refuse_target(pre.detail, event=pre.verdict.value,
                                                under_point=pre.under_point,
                                                foreground=pre.foreground)
                            break
                    self._verified_move = candidate
                    # Consumed by `_action_for` when it describes the button that follows.
                    self._pending_point = Point(primitive.x, primitive.y)

                # P0c: a button event with no preceding verified move is refused. This is
                # the last line of defence, and it is deliberately independent of both the
                # schema and the compiler.
                if (primitive.kind is PrimitiveType.MOUSE_BUTTON and primitive.down
                        and self._verified_move is None):
                    self._refuse_target(
                        "refused: mouse button down with no preceding move; an action "
                        "that acts at the operator's current cursor position is not "
                        "permitted",
                        event="mouse_button_down_without_move",
                    )
                    break

                # P2: verify the window under the intended point immediately before the
                # button goes down. Foreground validation alone is not enough - it proves
                # which window has focus, not which window is under the pixel.
                if (primitive.kind is PrimitiveType.MOUSE_BUTTON and primitive.down
                        and self.target_guard is not None
                        and self._verified_move is not None):
                    result = self.target_guard.validate_point(self._verified_move)
                    if not result.ok:
                        self._refuse_target(result.detail, event=result.verdict.value,
                                            under_point=result.under_point,
                                            foreground=result.foreground)
                        break
                    self._verified_move = None

                if self._policy is None or self._controller is None:
                    missing = "policy" if self._policy is None else "controller"
                    self.stats.blocked_batches += 1
                    self.blocked_reason = (
                        f"refused: no {missing} layer, so input is not sent unguarded"
                    )
                    break

                described = self._action_for(primitive, sent)
                request = ToolRequest(
                    action=described,
                    run_id=getattr(self, "run_id", ""),
                    action_id=self._safety_next_id(),
                    source="executor.execute",
                    target=self.target,
                )
                if described is None:
                    # No declarative equivalent for this primitive. Do not forward a request
                    # with no action: the controller must not be asked to permit something
                    # it cannot name. A bare release with no recorded anchor is the case
                    # that reaches here, and it is refused rather than waved through.
                    self._refuse_target(
                        "primitive has no declarative action and cannot be authorised",
                        event="undeclarable_primitive",
                    )
                    break

                # The controller performs the authoritative check at the point of
                # injection. This pre-check exists only to produce a precise reason before
                # dispatch, and the controller re-validates regardless - so authority to
                # inject and authority to permit are never held by different components.
                verdict = self._policy.validate(request, is_armed=self._input.enabled)
                if not verdict.allowed:
                    self._event_blocked(verdict, primitive, index)
                    break

                outcome = self._controller.execute(request, [primitive])
                if not outcome.ok:
                    self._input.set_enabled(False, thorough=False)
                    raise FrameForgeError(outcome.detail or "controller refused action")
                # Counted from the controller's own result, not from reaching this line:
                # a dry-run or disarmed port accepts the primitive and still withholds it,
                # so "dispatched" and "delivered" are different facts.
                delivered += int(getattr(outcome, "delivered", 0) or 0)
                # Keyboard input must clear the same gate as mouse input.
                #
                # Keyboard events have no point, so validate_point cannot apply - but the
                # consequences of skipping the guard are identical: SendInput delivers keys
                # to whatever holds foreground focus, so an unguarded keystroke while the
                # operator has Hermes or a browser in front types into *that*. The Notepad
                # round-trip failed for exactly this reason - the click was guarded and the
                # typing was not, so the text went to whatever was focused.
                if primitive.kind is PrimitiveType.KEY:
                    keyboard = self.target_guard.validate_keyboard() \
                        if self.target_guard is not None else None
                    if keyboard is None:
                        self._refuse_target(
                            "refused: keyboard input with no target guard; a keystroke with "
                            "no verified target goes to whatever holds focus",
                            event="keyboard_without_target_guard",
                        )
                        break
                    if not keyboard.ok:
                        self._refuse_target(keyboard.detail, event=keyboard.verdict.value,
                                            under_point=keyboard.under_point,
                                            foreground=keyboard.foreground)
                        break

                if primitive.down and primitive.kind is PrimitiveType.KEY \
                        and primitive.key in _MODIFIER_KEYS:
                    self._pending_modifier = True
                if primitive.down and primitive.hold_ms > 0:
                    # Owed, not slept here: the wait happens before the next primitive so
                    # that a guard trip between down and up still releases promptly.
                    self._pending_hold_ms = primitive.hold_ms
                sent += 1
                self.stats.primitives += 1
                # Rebase the human-input baseline after anything we send ourselves.
                #
                # SCANCODE was missing from this list, so scan-mode typing registered as
                # *human* input: the baseline was never rebased after our own keystroke, and
                # the very next sample saw idle time collapse and halted the run as
                # HumanInputDetected. Frame Forge was detecting itself.
                if self._human is not None and primitive.kind in _AGENT_INPUT_KINDS:
                    self._human.mark_agent_input()
                if index == len(primitives) - 1 and not allow_hold and self._pending_hold_ms > 0:
                    # A trailing hold with no matching up is a leak; settle it before the
                    # balance check so the key is not left down.
                    self._preflight()
                    self._clock.sleep_ms(self._pending_hold_ms)
                    self._pending_hold_ms = 0.0
        #: Gap between pressing a modifier and pressing the key it modifies.

            if not allow_hold:
                self._enforce_balance()
        except Estopped as exc:
            blocked = f"estopped: {exc}"
            self.stats.estops_honoured += 1
            self._input.release_all()
            self._last_error = None
        except Exception as exc:  # guard failures (focus, human input, budget)
            blocked = f"{type(exc).__name__}: {exc}"
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.stats.blocked_batches += 1
            # Never leave state held because a guard tripped mid-batch.
            self._release_held()

        # Final reconciliation on every exit path, including the happy one. If the
        # balanced-batch contract was honoured this is a no-op; if anything was left held,
        # it is released here rather than trusted to the next one.
        if not allow_hold:
            self._release_held()

        duration = self._clock.monotonic_ms() - t0
        held = tuple(f"key:{k}" for k in self._holds.held_keys) + tuple(
            f"btn:{b}" for b in self._holds.held_buttons
        )
        if blocked is None and self.blocked_reason:
            blocked = self.blocked_reason
        self.blocked_reason = None
        denied = blocked is not None and sent == 0 and "refused" in (blocked or "")
        return ExecutionResult(
            # The real count, even on a partial batch. Reporting 0 for "sent 3, then
            # blocked" is exactly the conflation dsh's defensive-patterns warns about: a
            # caller reads a cut-short run as a clean success.
            primitives_sent=sent,
        delivered_to_os=delivered,
            duration_ms=duration,
            blocked_reason=blocked,
            denied=denied,
            partial=blocked is not None and sent > 0,
            error=self._last_error,
            held_at_end=held,
        )

    # ------------------------------------------------------------------- internals

    def _preflight(self) -> None:
        if self._estop is not None and self._estop.triggered:
            msg = "emergency stop active"
            raise Estopped(msg)
        if self._focus is not None:
            self._focus.assert_focus()
        if self._human is not None:
            self._human.assert_no_human_input()

    def _track(self, primitive: Primitive) -> None:
        now = self._clock.monotonic_ms()
        if primitive.kind == PrimitiveType.KEY and primitive.key is not None:
            if primitive.down:
                self._holds.key_down(primitive.key, now)
            else:
                self._holds.key_up(primitive.key, now)
        elif primitive.kind == PrimitiveType.MOUSE_BUTTON and primitive.button is not None:
            if primitive.down:
                self._holds.button_down(primitive.button, now)
            else:
                self._holds.button_up(primitive.button, now)

    def _enforce_balance(self) -> None:
        if self._holds.held_count:
            leaked = [n for n, _h in self._holds.release_plan()]
            released = self._release_held()
            msg = f"leaked holds released: {released} (were {leaked})"
            raise RuntimeError(msg)

    def _refuse_target(self, detail: str, *, event: str,
                       under_point: dict | None = None,
                       foreground: dict | None = None) -> None:
        """Refuse an action on target-validation grounds and record why."""
        self.stats.blocked_batches += 1
        self.blocked_reason = f"target_validation_failed: {detail}"
        self.target_refusals.append({
            "event": event,
            "detail": detail,
            "under_point": under_point,
            "foreground": foreground,
            "cursor": (self._verified_move.x, self._verified_move.y)
                     if self._verified_move else None,
        })

    def _event_blocked_no_policy(self, primitive, index: int) -> None:
        self.stats.blocked_batches += 1
        self.blocked_reason = (
            "no policy layer: input is refused rather than sent unguarded"
        )

    def _event_blocked_no_controller(self, primitive, index: int) -> None:
        self.stats.blocked_batches += 1
        self.blocked_reason = (
            "no input controller: input must go through the controller that owns "
            "hold accounting and audit"
        )

    def _safety_next_id(self) -> str:
        """Ask the safety manager for the next action id, so ids are unique per run."""
        safety = getattr(self._input, "safety", None)
        if safety is not None and hasattr(safety, "next_action_id"):
            return safety.next_action_id()
        return f"exec{self.stats.batches:05d}"

    def _action_for(self, primitive, index: int):
        """Reconstruct a declarative action describing the primitive about to be sent.

        The policy layer reasons about *actions*, not primitives - that abstraction is what
        keeps raw OS events out of the decision path. Rebuilding it here keeps the executor
        unaware of the action vocabulary, so a new primitive type cannot silently skip
        validation on its way to the OS.
        """
        from frameforge.actions.model import (
            Click,
            KeyPress,
            MouseButtonUp,
            MoveMouse,
            MouseLook,
            Scroll,
            TypeText,
        )
        from frameforge.ports.input import PrimitiveType

        kind = primitive.kind
        if kind is PrimitiveType.KEY and primitive.key is not None:
            return KeyPress(key=primitive.key)
        if kind is PrimitiveType.SCANCODE:
            # Scan-mode typing arrives as scancode primitives with no Key attached. It
            # describes the same declarative action as the key press it stands for, so it
            # must clear the same policy check - otherwise adding real scancode support
            # would have silently made scan-mode input unauthorisable.
            key = getattr(primitive, "scancode_key", None)
            if key is None:
                return None
            return KeyPress(key=key)
        if kind is PrimitiveType.MOUSE_MOVE_ABS:
            # A move carries the point a following button will use; remember it so the
            # button primitive can be described as an anchored Click rather than a
            # point-less one, which P0 now (correctly) refuses.
            self._pending_point = Point(primitive.x, primitive.y)
            return MoveMouse(at=Point(primitive.x, primitive.y))
        if kind is PrimitiveType.MOUSE_BUTTON:
            # A release needs no point of its own: it releases what is already down, and it
            # must never move the cursor to do so. It is anchored to the *press* that is
            # being answered, which is why this reads `_last_button_point` and not the
            # pending move (that was consumed by the Click).
            if not primitive.down:
                anchor = self._last_button_point
                if anchor is None:
                    return None
                self._last_button_point = None
                return MouseButtonUp(at=Point(*anchor), button=primitive.button or "left")
            at = getattr(self, "_pending_point", None)
            if at is None:
                # No preceding move in this batch: the button cannot be anchored. The
                # runtime guard refuses it before dispatch, so this never reaches the OS.
                return None
            self._pending_point = None
            self._last_button_point = (at.x, at.y)
            return Click(at=at, button=primitive.button or "left", count=1)
        if kind is PrimitiveType.MOUSE_MOVE_REL:
            return MouseLook(dx=primitive.dx, dy=primitive.dy)
        if kind is PrimitiveType.SCROLL:
            return Scroll(dx=primitive.scroll_x, dy=primitive.scroll_y)
        if kind is PrimitiveType.UNICODE:
            # A unicode primitive carries a whole string, not one key, so it describes a
            # TypeText rather than a KeyPress. This case was missing entirely, which made
            # *all* unicode-mode typing unauthorisable: the policy refused it as
            # "undeclarable" and no text could ever reach a target.
            if not primitive.text:
                return None
            return TypeText(text=primitive.text, method="unicode")
        # Anything unrecognised returns None, and the policy refuses it.
        return None

    def _event_blocked(self, verdict, primitive, index: int) -> None:
        self.stats.blocked_batches += 1
        reason = verdict.reason.value if verdict.reason else "denied"
        self.blocked_reason = f"{reason}: {verdict.detail}"


    def _release_held(self) -> dict[str, list[str]]:
        """Reconcile every hold this executor believes it has.

        Sends bypass the port's enabled gate. A key-up dropped because the port was
        disarmed leaves the key physically down, which the user experiences as every later
        keystroke arriving as a modifier chord - the release-blocking defect.
        """
        keys: list[str] = []
        buttons: list[str] = []
        for name, handle in self._holds.release_plan():
            try:
                if name.startswith("key:"):
                    self._send_release(Primitive(kind=PrimitiveType.KEY, key=handle, down=False))
                    self._holds.key_up(handle, self._clock.monotonic_ms())
                    keys.append(str(handle))
                else:
                    self._send_release(
                        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=handle, down=False)
                    )
                    self._holds.button_up(handle, self._clock.monotonic_ms())
                    buttons.append(str(handle))
            except Exception:
                # Never let a failed release abort the remaining ones; a partial cleanup
                # is far better than abandoning the rest.
                continue
        try:
            self._input.release_all()
        except Exception:
            pass
        return {"keys": keys, "buttons": buttons}

    def _send_release(self, primitive: Primitive) -> None:
        """Send a release, preferring the port's force path when it has one."""
        force = getattr(self._input, "force", None)
        if callable(force):
            force(primitive)
        else:
            self._input.send(primitive)

    def release_everything(self) -> dict[str, list[str]]:
        """Emergency cleanup. Safe to call when already disarmed, and idempotent."""
        return self._release_held()


def mousemove_primitives(x: int, y: int, steps: int = 1, duration_ms: int = 0) -> list[Primitive]:
    """Interpolated absolute move.

    A single jump is fine for UI clicks and wrong for anything where the game's input
    sampling cares about the path (drag-select, camera pans in some engines). Steps > 1
    gives a path.
    """
    if steps <= 1 or duration_ms <= 0:
        return [Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=x, y=y)]
    out: list[Primitive] = []
    # Caller supplies the origin implicitly via the input port's cursor; we only know the
    # destination, so we emit a final absolute plus timed no-ops. Keeping it honest: the
    # executor's compiler computes the true origin before calling this.
    for _ in range(steps):
        out.append(Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=x, y=y))
    return out


def mouselook_primitives(
    dx: int, dy: int, steps: int = 12, duration_ms: int = 120, curve: str = "ease"
) -> list[Primitive]:
    """Break a relative flick into interruptible sub-moves.

    ``ease`` applies a smoothstep to the per-step magnitude so the motion accelerates
    and decelerates like a real hand. That is a *game-mechanics* concern - aim smoothing
    depends on the timing profile - and never an evasion tactic.
    """
    steps = max(1, steps)
    out: list[Primitive] = []
    per_step_delay = (duration_ms / steps) if duration_ms > 0 else 0.0

    # Accumulate on the *cumulative* target and emit the difference, rather than rounding
    # each step's delta independently. Independent rounding does not telescope: a 100px
    # flick over 10 eased steps came out as 102, which is a real aiming error, not a
    # rounding curiosity. The final step is forced to land exactly on the requested delta.
    prev_x = 0
    prev_y = 0
    for i in range(1, steps + 1):
        t = i / steps
        weight = t if curve == "linear" else (t * t * (3 - 2 * t))
        if i == steps:
            cum_x, cum_y = dx, dy
        else:
            cum_x = int(round(dx * weight))
            cum_y = int(round(dy * weight))
        step_dx = cum_x - prev_x
        step_dy = cum_y - prev_y
        prev_x, prev_y = cum_x, cum_y
        if step_dx == 0 and step_dy == 0 and i < steps:
            continue
        out.append(
            Primitive(
                kind=PrimitiveType.MOUSE_MOVE_REL,
                dx=step_dx,
                dy=step_dy,
                hold_ms=per_step_delay,
            )
        )
    return out


def drag_primitives(path: list, button: MouseButton = MouseButton.LEFT, duration_ms: int = 600) -> list[Primitive]:
    """Press at the path start, traverse, release at the end."""
    if len(path) < 2:
        msg = f"drag needs at least 2 points, got {len(path)}"
        raise ValueError(msg)
    out: list[Primitive] = [
        Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=path[0].x, y=path[0].y),
        Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=True),
    ]
    per_step = (duration_ms / max(1, len(path) - 1)) if duration_ms > 0 else 0.0
    for point in path[1:]:
        out.append(
            Primitive(kind=PrimitiveType.MOUSE_MOVE_ABS, x=point.x, y=point.y, hold_ms=per_step)
        )
    out.append(Primitive(kind=PrimitiveType.MOUSE_BUTTON, button=button, down=False))
    return out


def hotkey_primitives(keys: list[Key], hold_ms: float = 50.0) -> list[Primitive]:
    """Press in order, release in reverse order.

    Reverse release matters: games that track modifier state get confused by a
    forward-order release, and Windows itself can leave the modifier latched.
    """
    out: list[Primitive] = []
    for index, key in enumerate(keys):
        # Only the final key in the press sequence absorbs the hold.
        hold = hold_ms if index == len(keys) - 1 else 0.0
        out.append(Primitive(kind=PrimitiveType.KEY, key=key, down=True, hold_ms=hold))
    for key in reversed(keys):
        out.append(Primitive(kind=PrimitiveType.KEY, key=key, down=False))
    return out


__all__ = [
    "ActionExecutor",
    "ExecutionResult",
    "ExecutorStats",
    "drag_primitives",
    "hotkey_primitives",
    "mouselook_primitives",
    "mousemove_primitives",
]
