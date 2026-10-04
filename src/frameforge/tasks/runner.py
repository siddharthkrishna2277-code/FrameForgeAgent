"""Scenario runner: assembles the whole system and executes a scenario.

This is the composition root. Everything it wires is an interface plus an adapter, and
every one of those is swappable for a fake - which is why ``frameforge simulate`` can run
a complete scenario on a machine with no display.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from frameforge.actions.arm import (
    ArmToken,
    CaptureFrame,
    DEFAULT_EARLY_GAMEPLAY_PROFILE,
    ExecutionState,
    ExecutionStateMachine,
)
from frameforge.actions.compiler import ActionCompiler, ControlProfile, DisplayProfile
from frameforge.actions.controller import (
    ActionTarget,
    InputPolicy,
    PolicyConfig,
)
from frameforge.actions.runtime import build_live_gate
from frameforge.actions.target import ProtectedRegistry, TargetSession
from frameforge.actions.estop import EstopSwitch
from frameforge.actions.executor import ActionExecutor
from frameforge.actions.safety import InputSafetyManager, read_layout
from frameforge.actions.focus import FocusGuard, FocusPolicy, HumanInputDetector, HumanInputPolicy
from frameforge.actions.ledger import BudgetLedger, BudgetLimits
from frameforge.actions.model import (
    Action,
    Click,
    Hotkey,
    Intent,
    KeyPress,
    MouseLook,
    Scroll,
    TypeText,
)
from frameforge.config.settings import Settings
from frameforge.kernel.bus import EventLog, Redactor
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.director import RunDirector, RunOutcome
from frameforge.kernel.errors import PolicyError
from frameforge.kernel.events import EventKind
from frameforge.kernel.states import RunState
from frameforge.perception.assembler import AnchorSpec, FrameAssembler
from frameforge.perception.health import DisplayHealthMonitor, HealthVerdict
from frameforge.perception.verify import Condition, ScreenChanged, Verifier
from frameforge.planning.validator import PlanValidator, ValidationPolicy
from frameforge.ports.capture import CapturePort, Frame, Surface
from frameforge.ports.geometry import Point, Rect, Size
from frameforge.ports.input import Key, MouseButton
from frameforge.ports.ocr import OcrPort
from frameforge.ports.vision import VisionPort
from frameforge.ports.window import TargetSpec, WindowInfo, WindowPort
from frameforge.profiles.presets import get_preset
from frameforge.profiles.schema import GameProfileSpec
from frameforge.qa.report import Report, StepRecord, build_report, write_junit
from frameforge.store.runs import EvidenceStore, RunPaths, new_run
from frameforge.tasks.dsl import OnUnreachable, ScenarioDefinition, StepAction, TaskStep
from frameforge.tasks.loader import load_task


@dataclass(slots=True)
class RunnerWiring:
    """Everything a scenario needs, all injectable."""

    window: WindowPort
    capture: CapturePort
    ocr: OcrPort
    vision: VisionPort
    input_port: Any
    clock: ClockPort = field(default_factory=SystemClock)
    profile: GameProfileSpec | None = None
    templates: dict[str, np.ndarray] = field(default_factory=dict)
    surface: Surface | None = None


def build_control_profile(profile: GameProfileSpec | None) -> ControlProfile:
    """Resolve the control profile: a preset plus overrides.

    Overrides are applied per-intent, so a profile that is 90% ``fps`` and changes three
    bindings needs three lines rather than a whole new preset.
    """
    control = get_preset(profile.control_preset if profile else "ui")
    if profile:
        control.name = profile.name
        from frameforge.actions.compiler import Binding
        from frameforge.ports.input import Key as K

        for intent, spec in (profile.control_overrides or {}).items():
            if isinstance(spec, str):
                control.bind(intent, Binding(kind="key", keys=(K.parse(spec),)))
            elif isinstance(spec, dict):
                if "keys" in spec:
                    control.bind(intent, Binding(kind="key", keys=tuple(K.parse(k) for k in spec["keys"])))
                elif "key" in spec:
                    control.bind(intent, Binding(kind="key", keys=(K.parse(str(spec["key"])),)))
                elif "button" in spec:
                    control.bind(intent, Binding(kind="mouse_button", button=MouseButton(str(spec["button"]))))
        if profile.look_sensitivity is not None:
            control.look_sensitivity = profile.look_sensitivity
        if profile.text_method is not None:
            control.text_method = profile.text_method
    return control


def action_from_step(step: TaskStep, observation, control: ControlProfile) -> list[Action]:
    """Expand a declared step action into concrete logical actions.

    A click on an anchor resolves to the landmark's matched rect at *decision time*, not
    to authored coordinates - which is what keeps a profile resolution-independent.
    """
    action = step.action
    if action is None:
        return []

    match action.kind:
        case "none":
            return []
        case "intent":
            return [Intent(intent=action.intent, hold_ms=action.hold_ms, count=action.count)]
        case "key":
            return [KeyPress(key=Key.parse(action.key), count=action.count, hold_ms=action.hold_ms or 30)]
        case "hotkey":
            return [Hotkey(keys=[Key.parse(k) for k in action.keys], hold_ms=action.hold_ms or 50)]
        case "click":
            at = Point(int(action.at[0]), int(action.at[1])) if action.at else None
            return [Click(at=at, button=MouseButton(action.button), count=action.count,
                          hold_ms=action.hold_ms)]
        case "click_anchor" | "click_anchor_text":
            anchor = observation.anchor(action.anchor) if observation else None
            if anchor is None or anchor.rect is None:
                # Landmark not visible: emit nothing rather than clicking a stale guess.
                return []
            ox, oy = _click_offset(step, action.anchor)
            point = Point(
                int(anchor.rect.x + anchor.rect.width * ox),
                int(anchor.rect.y + anchor.rect.height * oy),
            )
            return [Click(at=point, button=MouseButton(action.button), count=action.count)]
        case "scroll":
            return [Scroll(dx=action.scroll_x, dy=action.scroll_y, steps=action.count)]
        case "mouselook":
            return [MouseLook(dx=action.dx, dy=action.dy, steps=action.steps,
                              duration_ms=action.duration_ms)]
        case "type":
            return [TypeText(text=action.text, method=control.text_method)]
    return []


_OFFSETS: dict[str, tuple[float, float]] = {}


def _click_offset(step: TaskStep, anchor: str) -> tuple[float, float]:
    return _OFFSETS.get(anchor, (0.5, 0.5))


class ScenarioRunner:
    """Builds the system from settings + scenario and executes it."""

    def __init__(
        self,
        settings: Settings,
        wiring: RunnerWiring,
        *,
        scenario: ScenarioDefinition,
    ) -> None:
        self.settings = settings
        self.wiring = wiring
        self.scenario = scenario
        self.clock = wiring.clock
        self.run_id = ""
        self.paths: RunPaths | None = None
        self.events: EventLog | None = None
        self.evidence: EvidenceStore | None = None
        self._last_observation = None
        self._compiler: ActionCompiler | None = None
        self._director: RunDirector | None = None
        self._health: DisplayHealthMonitor | None = None
        self._assembler: FrameAssembler | None = None
        self._focus: FocusGuard | None = None
        self._target: WindowInfo | None = None
        #: Set by ``frameforge replay`` to substitute a recorded planner.
        self._override_planner = None
        #: Set when this run started the target itself, for the report.
        #: Set when this run started the target itself. Recorded in the report so a reader
        #: can tell a launched run from one that attached to something the human started.
        self.launched_pid: int | None = None
        self.launch_command: str = ""
        self._input_baseline: str = ""
        self.policy: InputPolicy | None = None
        self.controller = None
        self.target_spec: ActionTarget | None = None
        self._expected_image_path: str = ""
        self._executor = None
        #: The single authority on live input. Every run gets one, and it starts OFF.
        self.machine = ExecutionStateMachine()
        self.target_session: TargetSession | None = None
        self.protected: ProtectedRegistry | None = None
        self.live_gate = None
        self.profile_spec = None
        self.input_health: dict = {}
        #: Central input-state owner. Sampled before any input and reconciled after.
        self.safety: InputSafetyManager | None = None
        self.input_health: dict[str, object] = {}
        #: Set when the caller already brought the target up (the CLI does, because it must
        #: resolve the window before it can build a capture surface).
        self._prelaunched = False
        self.step_records: list[StepRecord] = []
        self._captured_anchors: set[str] = set()
        self._last_present: set[str] = set()
        self._frame_counter = 0
        self.planner_degraded = False
        self.planner_name = "tier0_profile"

    # ------------------------------------------------------------------ assembly

    def prepare(self) -> None:
        """Create the run directory and wire every component.

        Samples the machine's input language *before* anything can be sent, so a run that
        changes it can restore it afterwards (guardrail G-ABS-10 / E).
        """
        s = self.settings
        self.run_id, self.paths = new_run(s.runs_dir, clock=self.clock)
        self.events = EventLog(
            self.paths.events,
            self.run_id,
            clock=self.clock,
            redactor=Redactor(),
        )
        self.events.append(
            EventKind.RUN_STARTED, "runner",
            f"scenario {self.scenario.name!r} starting",
            {"scenario": self.scenario.name, "objective": self.scenario.objective},
        )

        # Claim input state before anything can touch it, and make the estop's hook a
        # registered closable so cleanup is guaranteed to remove it.
        self.safety = InputSafetyManager(clock=self.clock)
        baseline = self.safety.begin_run()
        self.events.append(
            EventKind.RUN_STARTED, "runner",
            f"input baseline captured: {baseline.describe()}",
            layout=baseline.to_dict(),
        )

        self.evidence = EvidenceStore(
            self.paths,
            vision=self.wiring.vision,
            clock=self.clock,
            max_mb=s.evidence_max_mb,
        )

        # ------------------------------------------------------------- perception
        self._assembler = FrameAssembler(self.wiring.vision, clock=self.clock)
        self._health = DisplayHealthMonitor(self.wiring.window, self.wiring.vision, clock=self.clock)
        self._health.refresh_topology()

        # ----------------------------------------------------------------- target
        # Self-launch first, so a scenario is self-sufficient rather than depending on the
        # operator having started the application by hand.
        if not self._prelaunched and self.scenario.launch is not None and not self._override_planner:
            self.maybe_launch()

        self._focus = FocusGuard(
            self.wiring.window,
            policy=FocusPolicy(s.focus_policy),
            tolerance_ms=s.focus_tolerance_ms,
            clock=self.clock,
        )
        target_hwnd = None
        if self.wiring.profile is not None:
            target_hwnd = self._resolve_target()
        elif self.wiring.surface is not None:
            target_hwnd = self.wiring.surface.hwnd
        self._focus.set_target(target_hwnd)

        # Record the input language baseline before a single key can be synthesised, and
        # audit the *port's own* manager - not a private one. An earlier version created a
        # separate manager here, which meant the post-run audit described an object that had
        # never injected anything: "dispatches: 0" and a clean verdict, both meaningless.
        port_safety = getattr(self.wiring.input_port, "safety", None)
        if port_safety is not None:
            self.safety = port_safety
            self.safety.run_id = self.run_id
            # Target validation: actions are only permitted to reach the resolved window.
            if port_safety.target_hwnd is None and self._target is not None:
                port_safety.target_hwnd = self._target.hwnd
            baseline = self.safety.begin_run()
            self._input_baseline = baseline.describe()
            if self.events is not None:
                self.events.append(
                    EventKind.POLICY_CHECKED, "runner",
                    f"input language baseline captured: {baseline.describe()}",
                    layout=baseline.to_dict(),
                )
        else:
            # A fake port has no manager of its own. Keep one so cleanup and verification
            # still run and are still recorded, and say so honestly in the report.
            from frameforge.actions.safety import InputSafetyManager as _ISM

            self.safety = _ISM(clock=self.clock)
            self._input_baseline = "unavailable (input port exposes no safety manager)"
            self.input_health = {
                "available": False,
                "healthy": True,
                "reason": self._input_baseline,
            }

        # ---------------------------------------------------------------- budget
        ledger = BudgetLedger(
            limits=BudgetLimits(
                max_actions=s.max_actions,
                max_actions_per_minute=s.max_actions_per_minute,
                max_ai_calls=s.max_ai_calls,
                max_unknown_states=s.max_unknown_states,
                max_run_ms=s.max_run_ms,
            ),
            clock=self.clock,
        )

        # ------------------------------------------------- input authority
        # Built first, and locked. A run cannot inject anything until the operator walks it
        # through observe -> select -> ready -> countdown -> active, so the default posture of
        # every run is INPUT LOCKED.
        self.machine = ExecutionStateMachine(
            run_id=self.run_id or "unassigned",
            release_hook=self._release_all_input,
        )
        self.protected = ProtectedRegistry()
        self.protected.protect_current_process()
        for name in ("chrome_widgetwin_1", "progman", "shell_traywnd", "consolewindowclass",
                     "codetoplevel", "applicationframewindow", "#32770"):
            self.protected.protect_class(name)
        self._apply_protected_policy()
        self.machine.observe()

        # ------------------------------------------------- controller and policy
        # The InputController is the only component permitted to emit OS input. Live runs
        # get the Windows controller behind a default-deny policy; simulated runs get the
        # mock, which is why the entire suite can run with no desktop.
        self.policy = self._build_policy()
        self.controller = self._build_controller()
        self._wire_target_into_policy()

        # ---------------------------------------------------------------- guards
        estop = EstopSwitch(
            self.wiring.input_port,
            clock=self.clock,
            hotkey=s.estop_hotkey,
            sentinel_path=self.paths.abort_sentinel,
        )
        if self.safety is not None:
            self.safety.register_closable("estop-hook", estop)
        human = HumanInputDetector(
            self.wiring.window,
            policy=HumanInputPolicy(s.human_input_policy),
            clock=self.clock,
        )

        # -------------------------------------------------------------- executor
        if self.controller is None or self.policy is None:
            msg = (
                "ScenarioRunner refuses to build an executor without both a controller and "
                "a policy. Input that has not passed a policy check must not be "
                "constructible at all - this is the fail-closed path "
                "(docs/AI_GUARDRAILS.md G-ABS-11)."
            )
            raise RuntimeError(msg)
        executor = ActionExecutor(
            self.wiring.input_port,
            focus=self._focus,
            human=human,
            estop=estop,
            clock=self.clock,
            budget=ledger,
            controller=self.controller,
            policy=self.policy,
        )
        executor.run_id = self.run_id
        # Attach the gate's guard immediately. The gate was built when the target was
        # registered, which happens before the executor exists, so without this the guard
        # was on the gate but never on the thing that actually dispatches.
        if self.live_gate is not None:
            executor.target_guard = self.live_gate.guard
            self.live_gate.controller = self.controller

        # -------------------------------------------------------------- compiler
        control = build_control_profile(self.wiring.profile)
        display = DisplayProfile(
            reference_size=Size(*(self.wiring.profile.reference_size if self.wiring.profile else (1920, 1080))),
            viewport=self.wiring.profile.viewport if self.wiring.profile else (0.0, 0.0, 1.0, 1.0),
        )
        self._compiler = ActionCompiler(
            control, display,
            surface=self.wiring.surface,
            surface_size=self.wiring.surface.size if self.wiring.surface else None,
        )

        # ------------------------------------------------------------- director
        validator = PlanValidator(
            ValidationPolicy(
                max_plan_steps=s.ai_max_plan_steps,
                allowed_intents=frozenset(self.wiring.profile.allowed_intents) if self.wiring.profile else frozenset(),
                allow_irreversible=bool(self.wiring.profile.allow_irreversible if self.wiring.profile else False),
            )
        )
        director = RunDirector(
            event_log=self.events,
            assembler=self._assembler,
            verifier=Verifier(self.wiring.vision, clock=self.clock),
            executor=executor,
            validator=validator,
            ledger=ledger,
            clock=self.clock,
            estop=estop,
        )
        director.grace_seconds = s.grace_seconds
        director.min_confidence_to_act = s.min_confidence_to_act
        self._director = director
        self._ledger = ledger
        self._estop = estop
        # Built after the ledger exists: the AI planner charges its calls against it.
        self._planner = self._build_planner(s, ledger)
        director.planner = self._planner
        # Give the director what it needs to resume after a pause: the guard itself, and a
        # callable that re-resolves the target window.
        director._focus_guard = self._focus
        director.reacquire = self._acquire_again
        # The executor's target is the policy's ActionTarget. Without this the policy sees
        # a request with no target and refuses every action - a correct refusal of an
        # incorrect request, which reads as a broken run.
        executor.target = self.target_spec
        self._executor = executor
        # A recorded replay swaps the planner for the recording; this is the only step that
        # does so, which is why replay is a deliberate, explicit operation.
        if self._override_planner is not None:
            director.planner = self._override_planner
            self.planner_name = self._override_planner.capabilities().name

    def _input_profile(self):
        """The profile that governs live input.

        Prefers one declared by the scenario, falling back to the conservative
        movement-only default. There is no permissive fallback: an undeclared scenario
        gets the *least* capable profile, not the most.
        """
        declared = (self.scenario.metadata or {}).get("input_profile")
        if isinstance(declared, dict):
            from frameforge.actions.arm import InputProfile, SYSTEM_INTENTS

            return InputProfile(
                name=str(declared.get("name", "scenario_profile")),
                description=str(declared.get("description", "")),
                allowed_keys=frozenset(declared.get("allowed_keys", ())),
                max_hold_ms=dict(declared.get("max_hold_ms", {})),
                allow_left_click=bool(declared.get("allow_left_click", False)),
                allow_right_click=bool(declared.get("allow_right_click", False)),
                max_actions=int(declared.get("max_actions", 40)),
                system_intents=SYSTEM_INTENTS,
            )
        return DEFAULT_EARLY_GAMEPLAY_PROFILE

    def _release_all_input(self) -> None:
        """Release everything, whatever state the run is in. Never raises."""
        try:
            if self.wiring.input_port is not None:
                self.wiring.input_port.release_all(thorough=True)
        except Exception:
            pass

    def _apply_protected_policy(self) -> None:
        """Teach the policy about the windows this process owns and the shell.

        The registry is the authority; the policy needs the same list to refuse before the
        gate is even reached, so the two cannot disagree about what is protected.
        """
        if self.protected is None:
            return
        for name in self.protected.report().get("protected_classes", []):
            self.protected.protect_class(name)

    def _make_session(self, info, image_path: str) -> TargetSession:
        """Register the resolved window as the only thing this run may touch."""
        from frameforge.actions.target import dpi_for_window
        from frameforge.adapters.window.pywin32_window import monitor_topology_fingerprint

        origin = info.client_origin_screen
        size = info.client_rect.size if info.client_rect else None
        monitor_index, monitor_device = self._monitor_for(origin)
        return TargetSession(
            run_id=self.run_id or "unassigned",
            hwnd=int(info.hwnd),
            pid=int(info.pid),
            process_name=info.process_name,
            image_path=image_path,
            title=info.title,
            class_name=info.class_name,
            client_origin=(int(origin.x), int(origin.y)),
            client_size=size,
            monitor_index=monitor_index,
            monitor_device=monitor_device,
            dpi=dpi_for_window(int(info.hwnd)),
            approved_regions=self._approved_regions(),
            topology_fingerprint=monitor_topology_fingerprint(),
            require_foreground=True,
            protected_pids=frozenset(self.protected.report()["protected_pids"]) if self.protected else frozenset(),
        )

    def _monitor_for(self, origin) -> tuple[int, str]:
        """Which monitor a window sits on, by containment of its client origin."""
        try:
            for monitor in self.wiring.window.monitors_detailed():
                if monitor.rect.contains(
                    __import__("frameforge.actions.coordinates", fromlist=["ScreenPx"]).ScreenPx(
                        int(origin.x), int(origin.y))
                ):
                    return monitor.index, monitor.device_name
        except Exception:
            pass
        return 0, ""

    def _approved_regions(self) -> tuple[tuple[float, float, float, float], ...]:
        """Regions a scenario may click, in client fractions.

        Defaults to the whole client area. A scenario that needs something narrower should
        say so here rather than relying on a coordinate that happens to work today.
        """
        declared = (self.scenario.metadata or {}).get("approved_regions")
        if isinstance(declared, list) and declared:
            out = []
            for region in declared:
                if isinstance(region, list | tuple) and len(region) == 4:
                    out.append(tuple(float(v) for v in region))
            if out:
                return tuple(out)
        return ((0.0, 0.0, 1.0, 1.0),)

    # ------------------------------------------------------------- live input

    def arm_for_live_input(
        self,
        *,
        attestation: str,
        profile=None,
        countdown_ms: float = 4000.0,
    ):
        """Operator entry point. The only way a run may ever reach ACTIVE.

        Refuses unless there is a registered target session and a live capture. There is
        deliberately no implicit or convenience arming: a caller that wants input has to
        supply the user's own words.
        """
        from frameforge.actions.arm import BlockReason as BR, BlockResult as Res

        session = self.target_session
        if session is None:
            return Res.deny(BR.SESSION_MISSING,
                            "no target session; run with --profile so the target is registered")
        observation = self._last_observation
        if observation is None:
            return Res.deny(BR.CAPTURE_UNAVAILABLE, "no capture for the registered target")
        from frameforge.actions.arm import CaptureFrame

        frame = CaptureFrame(
            frame_id=f"run-{self.run_id}", run_id=self.run_id or "",
            session_id=getattr(session, "session_id", ""),
            target_hwnd=session.hwnd, target_pid=session.pid,
            captured_mono_ms=(self.clock.monotonic_ms()
                               if hasattr(self.clock, "monotonic_ms") else 0.0),
            width=int(observation.frame.array.shape[1]),
            height=int(observation.frame.array.shape[0]),
            client_rect=session.client_rect.as_tuple(),
            monitor_index=session.monitor_index, monitor_device=session.monitor_device,
            dpi=session.dpi, topology_fingerprint=session.topology_fingerprint,
            healthy=observation.usable,
        )
        self.machine.mark_ready(frame)
        token = ArmToken(
            session_id=getattr(session, "session_id", ""),
            profile_name=(profile or self._input_profile()).name,
            user_attestation=attestation,
        )
        if not attestation.strip():
            return Res.deny(BR.GAMEPLAY_UNCONFIRMED,
                            "the operator must confirm active gameplay before arming")
        result = self.machine.begin_countdown(token, countdown_ms=countdown_ms)
        if result.blocked:
            return result
        self._build_live_gate()
        return result

    def confirm_live_input(self, gameplay=None):
        """Countdown finished. Re-validate everything before ACTIVE."""
        result = self.machine.confirm_active(gameplay)
        self._build_live_gate()
        return result

    def _build_live_gate(self) -> None:
        """(Re)assemble the gate around the current session and target."""
        self.live_gate = build_live_gate(
            machine=self.machine,
            session=self.target_session,
            policy=self.policy,
            controller=getattr(self, "controller", None),
            registry=self.protected,
            topology_fingerprint=(self.target_session.topology_fingerprint
                                  if self.target_session else ""),
        )
        if self._executor is not None:
            # The executor is created after the target is wired, so attach here too. A gate
            # that exists but is not on the executor is not on the path.
            self._executor.target_guard = self.live_gate.guard
            self._executor.run_id = self.run_id or ""

    def live_input_status(self) -> dict:
        """What the CLI and a future GUI read. Never claims more than is true."""
        status = {
            "state": self.machine.state.value,
            "input_locked": self.machine.state is not ExecutionState.ACTIVE,
            "banner": self.machine.banner(),
            "session": (self.target_session.to_dict() if self.target_session else None),
            "protected": (self.protected.report() if self.protected else None),
            "decisions": (self.live_gate.decisions[-20:] if self.live_gate else []),
        }
        return status

    def _build_policy(self) -> InputPolicy:
        """Default-deny policy: live input is permitted only to a named target process."""
        config = PolicyConfig(
            require_foreground=True,
            enforce_bounds=True,
            allow_right_click=bool(
                (self.scenario.metadata or {}).get("allow_right_click", False)
            ),
            max_hold_ms=float(
                (self.scenario.metadata or {}).get("max_hold_ms", 2000.0)
            ),
        )
        if self.wiring.profile is not None and self.wiring.profile.target.process_name:
            # Narrow the allow-list to this run's declared target.
            config.allow_processes = frozenset(
                {self.wiring.profile.target.process_name.lower()}
            )
        return InputPolicy(config)

    def _build_controller(self):
        """Live controller for a real port; mock for a fake one."""
        from frameforge.actions.controller import build_controller

        return build_controller(
            self.wiring.input_port,
            policy=self.policy,
            verbose_events=bool(self.settings.verbose),
        )

    def _wire_target_into_policy(self) -> None:
        """Point the policy and executor at the resolved window.

        Until this runs, live input is refused: there is no allow-listed window, so the
        correct behaviour is to do nothing.
        """
        if self.wiring.profile is None or self._target is None:
            return
        info = self._target
        image_path = ""
        expected = ""
        if self.wiring.profile is not None:
            expected = self.wiring.profile.expected_image_path
        resolver = getattr(self.wiring.window, "process_image_path", None)
        if callable(resolver):
            try:
                image_path = resolver(info.pid) or ""
            except Exception:
                image_path = ""
        if expected and image_path and image_path.lower() != expected.lower():
            # Record the disagreement rather than raising here: the policy refuses it on the
            # first action, and the report says why. A hard failure at resolve time would
            # deny a run whose target merely moved directories.
            if self.events is not None:
                self.events.append(
                    EventKind.PERCEPTION_DEGRADED, "runner",
                    f"target image path {image_path!r} differs from the profile's "
                    f"expected {expected!r}; live input will be refused",
                    actual=image_path, expected=expected,
                )
        spec = ActionTarget(
            hwnd=info.hwnd,
            pid=info.pid,
            process_name=info.process_name,
            class_name=info.class_name,
            title=info.title,
            bounds=info.client_rect,
            image_path=image_path,
        )
        self._expected_image_path = expected
        self.policy.configure_for_target(spec)
        self.target_session = self._make_session(info, image_path)
        self.machine.select_target(self.target_session, self._input_profile())
        if getattr(self.wiring.input_port, "safety", None) is not None:
            self.wiring.input_port.safety.target_hwnd = info.hwnd
        if self.controller is not None and hasattr(self.controller, "_port"):
            self.controller._port.safety.target_hwnd = info.hwnd
        self.target_spec = spec

        # The gate exists from the moment a target is registered, so the executor always has
        # a guard and no caller can reach the port before one exists. Building it only at
        # arm time left the entire run unguarded.
        self._build_live_gate()

    def _build_planner(self, settings, ledger):
        """Choose the planner. Tier 0 is the default and is complete on its own.

        The AI tier is opt-in twice over: ``ai_enabled`` must be set *and* a provider must
        be usable. If the provider is configured but broken, the run says so and continues
        on Tier 0 rather than failing - a QA tool that stops working because a third-party
        API is down is not a QA tool (guardrail G-DEG-02).
        """
        if not settings.ai_enabled:
            self.planner_name = "tier0_profile"
            return None

        from frameforge.planning.ai import build_ai_planner

        allowed = frozenset(self.wiring.profile.allowed_intents) if self.wiring.profile else frozenset()
        try:
            planner = build_ai_planner(
                settings, allowed_intents=allowed, ledger=ledger, clock=self.clock
            )
        except Exception as exc:
            self.planner_degraded = True
            self.planner_name = "tier0_profile (ai unavailable)"
            if self.events is not None:
                self.events.append(
                    EventKind.PLANNER_DEGRADED, "runner",
                    f"AI tier unavailable, continuing on tier 0: {type(exc).__name__}: {exc}",
                    reason=str(exc),
                )
            return None
        self.planner_name = f"tier1_ai:{planner.capabilities().name}"
        if self.events is not None:
            self.events.append(
                EventKind.PLANNER_CALL, "runner",
                f"AI planner active: {self.planner_name}",
                planner=self.planner_name, model=settings.ai_model,
                allowed_intents=sorted(allowed),
            )
        return planner

    def maybe_launch(self, clock=None) -> bool:
        """Start the target if it is not already running, then wait for it to be *ready*.

        Delegates to the shared launch helper so the authorisation policy, the argv
        handling and the reporting live in one place (guardrail G-BLAST-02).
        """
        from frameforge.tasks.launch import ensure_target, is_authorised, wait_for_landmark

        if not is_authorised(self.scenario):
            if self.scenario.launch is not None and self.events is not None:
                self.events.append(
                    EventKind.POLICY_REFUSED, "runner",
                    "scenario declares a launch spec but not allow_direct_launch=true; refusing",
                    executable=self.scenario.launch.executable,
                )
            return False

        if self._resolve_target() is not None:
            return True

        hwnd = ensure_target(
            self.scenario.launch,
            lambda: self._resolve_target(),
            clock=clock or self.clock,
            events=self.events,
        )
        if hwnd is None:
            return False
        self.launched_pid = hwnd
        self.launch_command = self.scenario.launch.describe()

        # The window existing is not the same as the application being ready.
        return wait_for_landmark(
            self.scenario.launch.ready_landmark,
            self.fetch_observation,
            clock=self.clock,
            events=self.events,
            timeout_ms=self.scenario.launch.timeout_ms,
        )

    def _acquire_again(self) -> None:
        """Re-resolve the target after a pause, and re-point the focus guard at it."""
        assert self._focus is not None
        if self.wiring.profile is None:
            return
        hwnd = self._resolve_target()
        if hwnd is not None:
            self._focus.set_target(hwnd)
            # A new hwnd means a new identity, so the policy target must follow it.
            self._wire_target_into_policy()
            if getattr(self, "_executor", None) is not None:
                self._executor.target = self.target_spec

    def _resolve_target(self) -> int | None:
        assert self.wiring.profile is not None
        info = self.wiring.window.find_window(self.wiring.profile.target)
        if info is None:
            self.events and self.events.append(
                EventKind.WINDOW_LOST, "runner",
                f"target not found: {self.wiring.profile.target.describe()}",
                {"spec": self.wiring.profile.target.describe()},
            )
            return None
        self._target = info
        self.events.append(
            EventKind.WINDOW_FOUND, "runner", f"target acquired: {info.describe()}",
            {"hwnd": info.hwnd, "pid": info.pid, "title": info.title,
             "class": info.class_name, "process": info.process_name},
        )
        if self.wiring.surface is None:
            size = info.client_rect.size if info.client_rect else Size(1920, 1080)
            self.wiring.surface = Surface(
                kind=self.wiring.surface.kind if self.wiring.surface else _kind_for(self.settings),
                size=size,
                offset_x=info.client_origin_screen.x,
                offset_y=info.client_origin_screen.y,
                hwnd=info.hwnd,
            )
        return info.hwnd

    # ----------------------------------------------------------------- observe

    def fetch_observation(self):
        """Grab a frame and assemble an observation. The only perception entry point."""
        assert self._assembler is not None and self._health is not None
        assert self.wiring.capture is not None

        # Occlusion first: if something else is on screen in the target's rect, every
        # other perception result is a mixture of two applications, so there is no point
        # paying for OCR on it.
        if self._target is not None and self._target.client_rect is not None:
            occlusion = self._health.check_occlusion(
                self._target.hwnd,
                self._target.client_origin_screen,
                self._target.client_rect.size,
            )
            if occlusion.verdict is HealthVerdict.PAUSE and self._try_lift_occlusion():
                # The target was covered, we were authorised to raise it, and it worked.
                # Fall through and observe normally.
                pass
            elif occlusion.verdict is HealthVerdict.PAUSE:
                # check_occlusion returns a HealthReport, not an OcclusionInfo: the covering
                # window names live in the report's detail string, the fraction in metrics.
                self.events and self.events.append(
                    EventKind.PERCEPTION_DEGRADED, "runner",
                    f"target occluded: {occlusion.detail}",
                    occluded_fraction=occlusion.metrics.get("occluded_fraction"),
                    detail=occlusion.detail,
                )
                self._last_observation = None
                return None
            if occlusion.verdict is HealthVerdict.WATCH:
                self.events and self.events.append(
                    EventKind.PERCEPTION_DEGRADED, "runner",
                    f"target partly occluded: {occlusion.detail}",
                    occluded_fraction=occlusion.metrics.get("occluded_fraction"),
                )

        frame = self.wiring.capture.grab()
        status = self.wiring.capture.status()
        health = self._health.observe(frame, status)
        if frame is None:
            return None

        window_info = self.wiring.window.foreground()
        if self._target is not None:
            current = self.wiring.window.window_info(self._target.hwnd)
            if current is None:
                self.events and self.events.append(
                    EventKind.WINDOW_LOST, "runner", "target window vanished", {"hwnd": self._target.hwnd}
                )
            elif current.identity() != self._target.identity():
                self.events and self.events.append(
                    EventKind.WINDOW_IDENTITY_CHANGED, "runner",
                    "target identity changed; treating as target loss",
                    {"before": list(self._target.identity()), "after": list(current.identity())},
                )
                self._focus.set_target(current.hwnd)
            window_info = current

        ocr = self._assembler.read_text(frame, self.wiring.ocr)
        anchors = self._assembler.evaluate_anchors(frame, self._anchor_specs(frame), ocr)
        probes = self._color_probes(frame, anchors)

        observation = self._assembler.assemble(
            frame,
            window=window_info,
            ocr=ocr,
            anchors=anchors,
            color_probes=probes,
            health=health,
            topology_epoch=self._health.epoch,
        )
        self._last_observation = observation
        if self._director is not None:
            self._director.set_observation(observation)
        self._maybe_capture_evidence(observation)
        return observation

    def _try_lift_occlusion(self) -> bool:
        """Raise the target out from under whatever is covering it.

        Only ever attempted under an explicit ``refocus`` policy - taking the foreground
        away from another application is a courtesy, not a default. This is the recovery
        half of occlusion detection: a notification, a tooltip or a background app that
        grabs focus mid-run would otherwise end the scenario, when in fact the correct
        response is to ask for the target back.

        Bounded attempts, because a target that cannot be raised (a modal dialog owned by
        another app, a minimised window) must report that fact rather than spin.
        """
        from frameforge.actions.focus import FocusPolicy

        if self._focus is None or self._focus.policy is not FocusPolicy.REFOCUS:
            return False
        if self._target is None:
            return False
        for attempt in range(2):
            if not self.wiring.window.set_foreground(self._target.hwnd):
                self.clock.sleep_ms(150)
                continue
            self.clock.sleep_ms(200)
            info = self.wiring.window.window_info(self._target.hwnd)
            if info is None or info.client_rect is None:
                return False
            check = self._health.check_occlusion(
                info.hwnd, info.client_origin_screen, info.client_rect.size
            )
            if check.verdict is not HealthVerdict.PAUSE:
                if self.events is not None:
                    self.events.append(
                        EventKind.PERCEPTION_DEGRADED, "runner",
                        f"raised the target out from under an occluder after "
                        f"{attempt + 1} attempt(s)",
                        attempts=attempt + 1,
                    )
                return True
        return False

    def _maybe_capture_evidence(self, observation) -> None:
        """Save frames the scenario's evidence policy asks for.

        Deliberately narrow: a frame on failure, on UNKNOWN, and on state change - not one
        per epoch. A full-rate capture would fill the disk within an hour and the frames
        nobody opens are the overwhelming majority. The stored frames are the ones a
        report links to, so they are the ones worth keeping.
        """
        if self.evidence is None:
            return
        policy = self.scenario.evidence
        tags: list[str] = []
        present = set(observation.present_anchors())

        # A landmark set we have not photographed yet.
        new = present - self._captured_anchors
        if new and policy.capture_key_frames:
            for name in sorted(new)[:3]:
                tags.append(f"state_{name}")
            self._captured_anchors |= new

        if policy.periodic_every_n:
            self._frame_counter += 1
            if self._frame_counter % policy.periodic_every_n == 0:
                tags.append(f"periodic_{self._frame_counter}")

        for tag in tags:
            self.evidence.save_frame(observation.frame.array, tag, key=True)

        if policy.ocr_snapshots and present - self._last_present:
            try:
                self.evidence.save_ocr(
                    f"state_{sorted(present)[0] if present else 'unknown'}",
                    {"lines": [line.text for line in observation.ocr.lines]},
                )
            except Exception:
                pass
        self._last_present = present

    def capture_failure_evidence(self, step_name: str, observation) -> None:
        """Called by the director path when a step does not pass."""
        if self.evidence is None or observation is None:
            return
        policy = self.scenario.evidence
        if policy.capture_on_failure:
            self.evidence.save_frame(observation.frame.array, f"FAIL_{step_name}", key=True)
        if policy.capture_on_unknown:
            self.evidence.save_frame(observation.frame.array, f"UNKNOWN_{step_name}", key=True)

    def _anchor_specs(self, frame: Frame) -> list[AnchorSpec]:
        if self.wiring.profile is None:
            return []
        size = frame.size
        specs: list[AnchorSpec] = []
        for landmark in self.wiring.profile.landmarks:
            region = landmark.region.to_rect(size) if landmark.region else None
            specs.append(
                AnchorSpec(
                    name=landmark.name,
                    kind=landmark.kind,
                    rect=region,
                    template=self.wiring.templates.get(landmark.name),
                    pattern=landmark.pattern,
                    probe=landmark.probe,
                    threshold=landmark.threshold,
                )
            )
            if landmark.click_offset != (0.5, 0.5):
                _OFFSETS[landmark.name] = landmark.click_offset
        return specs

    def _color_probes(self, frame: Frame, anchors) -> tuple[tuple[str, bool, float], ...]:
        out: list[tuple[str, bool, float]] = []
        for anchor in anchors:
            if anchor.name.endswith("_probe"):
                out.append((anchor.name, anchor.present, anchor.score))
        return tuple(out)

    # -------------------------------------------------------------------- run

    def run(self) -> tuple[RunOutcome, Report]:
        assert self._director is not None and self.paths is not None
        plan = self._build_plan()

        try:
            outcome = self._director.run(
                plan,
                objective=self.scenario.objective,
                fetch_observation=self.fetch_observation,
            )
        finally:
            # Guaranteed cleanup on *every* exit path: success, failure, exception,
            # cancellation, timeout, estop. Without this, a crash between dispatch and
            # release could leave a key down - which is the defect class this whole module
            # exists to eliminate.
            self._finalise_input_state()

        # On any non-pass verdict, photograph the state that produced it. This is the
        # single most useful artefact in the whole run directory.
        policy = self.scenario.evidence
        for verdict in outcome.verdicts:
            if verdict.disposition == "pass":
                continue
            tag = "FAIL" if verdict.disposition == "fail" else "UNKNOWN"
            index = outcome.verdicts.index(verdict)
            name = (self.scenario.steps[index].name
                    if index < len(self.scenario.steps) else f"step_{index}")
            if tag == "FAIL" and not policy.capture_on_failure:
                continue
            if tag == "UNKNOWN" and not policy.capture_on_unknown:
                continue
            if self.evidence is not None and self._last_observation is not None:
                self.evidence.save_frame(
                    self._last_observation.frame.array, f"{tag}_{name}", key=True
                )

        # Never report success if the operator's input state could not be verified.
        health = self.input_health or {}
        if health.get("available") and not health.get("healthy", True):
            outcome.state = RunState.CLEANUP_FAILED
            reason = (
                "input cleanup could not be verified; run 'frameforge recover-input'. "
                + json.dumps(health.get("post_run_check", {}), default=str)[:400]
            )
            outcome.error = (outcome.error + " | " + reason) if outcome.error else reason
            if self.events is not None:
                self.events.append(
                    EventKind.PERCEPTION_DEGRADED, "runner",
                    "run marked cleanup_failed: input state not confirmed clean",
                    state=str(outcome.state), check=health.get("post_run_check", {}),
                )

        self.step_records = self._collect_step_records(outcome)
        report = self._build_report(outcome)
        report.save(self.paths)
        if self.settings.write_junit:
            write_junit(report, self.paths.junit)
        if self._director.plan_trace:
            import json

            self.paths.plan.write_text(
                "\n".join(json.dumps(t, default=str) for t in self._director.plan_trace) + "\n",
                encoding="utf-8",
            )
        return outcome, report

    def _finalise_input_state(self) -> None:
        """Post-run input hygiene. Runs on every exit path, including exceptions.

        Order matters: release first, restore the input language second, verify third.
        The report carries the result, so a run that left anything down is visible rather
        than silent.
        """
        if self.safety is None:
            return
        report: dict[str, object] = {}
        try:
            report["release"] = self.safety.release_all()
        except Exception as exc:
            report["release_error"] = str(exc)
        try:
            report["layout"] = self.safety.restore_layout()
        except Exception as exc:
            report["layout_error"] = str(exc)
        try:
            report["check"] = self.safety.post_run_check()
        except Exception as exc:
            report["check_error"] = str(exc)

        health = report.get("check") or {}
        # Normalise so the report and the CLI always see the same keys.
        report["healthy"] = bool(health.get("healthy"))
        report["post_run_check"] = health
        report["baseline_layout"] = getattr(self, "_input_baseline", "")
        report["audit"] = "input_audit.json"
        self.input_health = report
        if self.paths is not None:
            try:
                self.safety.write_audit(self.paths.root / "input_audit.json")
            except Exception:
                pass
        if self.events is not None:
            healthy = bool(health.get("healthy"))
            self.events.append(
                EventKind.RUN_FINISHED if healthy else EventKind.PERCEPTION_DEGRADED,
                "runner",
                ("input state verified clean" if healthy
                 else "INPUT STATE NOT CLEAN - run 'frameforge recover-input'"),
                healthy=healthy,
                stuck=health.get("os_modifiers_down"),
                layout_now=health.get("layout_now"),
                layout_baseline=health.get("layout_baseline"),
            )

    def _build_plan(self) -> list[dict]:
        """Turn the scenario into the director's step dicts.

        A step whose ``require`` landmarks are absent is marked for the unreachable policy
        rather than executed blindly - that is how "the game never got to the menu" becomes
        a FAIL with a reason instead of a click into the void.
        """
        plan: list[dict] = []
        # Setup first. These establish a known starting state and are deliberately allowed
        # to fail without failing the run.
        for step in self.scenario.setup:
            plan.append({
                "name": f"setup:{step.name}",
                "task_step": step,
                "objective": step.name,
                "actions": lambda st=step: self._resolve_step_actions(st),
                "condition": lambda st=step: self._condition_for(st, self._last_observation),
                "compiler": self._compiler,
                "attempts": 1 if not step.is_retryable() else step.attempts,
                "settle_ms": step.settle_ms,
                "timeout_ms": step.timeout_ms,
                "poll_until_true": False,
                "is_setup": True,
            })
        for step in self.scenario.steps:
            plan.append({
                "name": step.name,
                "task_step": step,
                "objective": self.scenario.objective or step.name,
                # Zero-argument callables: resolved by the director at execution time so a
                # click on a landmark uses *this* epoch's matched rect rather than a value
                # guessed when the plan was built.
                "actions": lambda st=step: self._resolve_step_actions(st),
                "condition": lambda st=step: self._condition_for(st, self._last_observation),
                "compiler": self._compiler,
                # A non-idempotent step gets a single attempt. Repeating a typing step
                # after a failed postcondition typed the probe string three times before
                # this was found against real Notepad.
                "attempts": step.attempts if step.is_retryable() else 1,
                "retryable": step.is_retryable(),
                "settle_ms": step.settle_ms,
                "timeout_ms": step.timeout_ms,
                # A `settle` step is a wait, not an assertion: poll until the condition
                # holds or the step's timeout elapses.
                "poll_until_true": step.kind.value == "settle",
            })
        return plan

    def _resolve_step_actions(self, step: TaskStep) -> list[Action]:
        observation = self.fetch_observation()
        if observation is None:
            return []
        if self.wiring.profile is not None:
            missing = [name for name in step.require if not observation.anchor_present(name)]
            if missing:
                self._record_unreachable(step, missing)
                return []
        assert self._compiler is not None
        control = build_control_profile(self.wiring.profile)
        return action_from_step(step, observation, control)

    def _condition_for(self, step: TaskStep, observation) -> Condition:
        from frameforge.tasks.dsl import build_condition

        args = dict(step.verify_args)
        if step.verify == "anchor_visible" and "anchor" not in args and step.expect:
            args["anchor"] = step.expect[0]
        if step.verify == "anchor_absent" and "anchor" not in args and step.require:
            args["anchor"] = step.require[0]
        try:
            return build_condition(step.verify, args)
        except KeyError:
            return ScreenChanged()

    def _record_unreachable(self, step: TaskStep, missing: list[str]) -> None:
        if self.events is not None:
            self.events.append(
                EventKind.VERIFIER_UNKNOWN, "runner",
                f"step {step.name!r} unreachable: missing {missing}",
                {"step": step.name, "missing": missing, "policy": str(step.on_unreachable)},
            )

    def _collect_step_records(self, outcome: RunOutcome) -> list[StepRecord]:
        """Pair verdicts with the actions that produced them."""
        by_step: dict[str, list] = {}
        for trace in self._director.plan_trace if self._director else []:
            by_step.setdefault(trace["step"], []).append(trace["action"])

        records: list[StepRecord] = []
        names = [f"setup:{s.name}" for s in self.scenario.setup]
        names += [s.name for s in self.scenario.steps]
        frames_dir = self.paths.frames if self.paths else None
        for index, verdict in enumerate(outcome.verdicts):
            name = names[index] if index < len(names) else f"verdict_{index}"
            evidence_paths: list[str] = []
            if frames_dir is not None:
                for pattern in (f"*FAIL_{name}.png", f"*UNKNOWN_{name}.png", f"*state_*.png"):
                    evidence_paths.extend(
                        str(p.relative_to(self.paths.root).as_posix())  # type: ignore[union-attr]
                        for p in sorted(frames_dir.glob(pattern))[-1:]
                    )
            records.append(
                StepRecord(
                    name=name,
                    disposition=str(verdict.disposition),
                    condition=verdict.condition,
                    detail=verdict.detail,
                    expected=verdict.expected,
                    actual=verdict.actual,
                    confidence=verdict.confidence,
                    frame_index=verdict.frame_index,
                    frame_hash=verdict.frame_hash,
                    mono_ms=verdict.mono_ms,
                    actions=by_step.get(name, []),
                    evidence=evidence_paths,
                )
            )
        return records

    def _build_report(self, outcome: RunOutcome) -> Report:
        assert self.paths is not None
        env = self._health.topology if self._health else None
        return build_report(
            run_id=self.run_id,
            generated_at=self.clock.iso_now(),
            state=outcome.state,
            verdicts=outcome.verdicts,
            steps=self.step_records,
            plan_trace=self._director.plan_trace if self._director else [],
            events=self.events.events if self.events else [],
            objective=self.scenario.objective,
            scenario=self.scenario.name,
            build_id=self.scenario.build_id,
            commit=self.scenario.commit,
            launch_mode="direct" if self.launched_pid else self.scenario.launch_mode,
            planner=self.planner_name,
            planner_degraded=self.planner_degraded,
            ai_calls=self._ledger.ai_calls_used if hasattr(self, "_ledger") else 0,
            budget=self._ledger.snapshot() if hasattr(self, "_ledger") else {},
            environment={
                "platform": "windows",
                "monitors": (
                    [f"{m.device_name} {m.rect.as_tuple()} primary={m.is_primary}"
                     for m in env.monitors] if env else []
                ),
                "virtual_desktop": env.virtual_rect.as_tuple() if env else None,
                "session": str(self.wiring.window.session_state()),
                "capture_backend": self.settings.capture_backend,
                "ocr_backend": self.wiring.ocr.capabilities().primary,
                "settings": self.settings.redacted_dict(),
            },
            reproduction={
                "command": f"frameforge run --task {self.scenario.name}",
                "launch_mode": (
                    "direct" if self.launched_pid else self.scenario.launch_mode
                ),
                "launch_command": self.scenario.launch.describe() if self.scenario.launch else "",
                "launched_pid": self.launched_pid,
                "scenario": str(self.scenario.name),
                "launch_mode": self.scenario.launch_mode,
                "profile": self.wiring.profile.name if self.wiring.profile else None,
                "seed": self.scenario.seed,
                "recorded_plan": "plan.jsonl in this directory",
                "note": "Replay with 'planner: recorded' for a deterministic re-run.",
            },
            input_safety=dict(self.input_health or {}),
            intents=[i for i in (self._director._intents if self._director else [])
                     if i.get("status") != "pending"],
            unresolved_intents=[
                {"intent_id": i["intent_id"], "step": i["step"], "actions": i["actions"]}
                for i in (self._director._intents if self._director else [])
                if i.get("status") == "pending"
            ],
            loop_guard=self._director.loop_guard.report() if self._director else {},
            evidence={
                "events": str(self.paths.events),
                "report_json": str(self.paths.report_json),
                "frames_dir": str(self.paths.frames),
                "store_stats": self.evidence.stats if self.evidence else {},
            },
            errors=([outcome.error] if outcome.error else [])
            + ([] if self.input_health.get("healthy", True)
               else ["INPUT STATE NOT CONFIRMED CLEAN - see input_audit.json"]),
            expected_failures=self.scenario.expected_failures,
        )


def _kind_for(settings: Settings):
    from frameforge.ports.capture import SurfaceKind

    return {
        "window": SurfaceKind.WINDOW,
        "monitor": SurfaceKind.MONITOR,
        "desktop": SurfaceKind.DESKTOP,
    }[settings.capture_scope]


__all__ = ["RunnerWiring", "ScenarioRunner", "action_from_step", "build_control_profile"]
