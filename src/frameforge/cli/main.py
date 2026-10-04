"""CLI entry point.

Design note: ``simulate`` runs the *real* director, assembler, verifier, compiler and
executor against fakes. It is not a mock - it is the same code path with the Windows
adapters swapped out. That is what makes it useful for CI and for developing the engine on
a machine with no display, and it is why the whole engine is built behind ports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from frameforge.config.settings import Settings, load_settings
from frameforge.kernel.errors import FrameForgeError, PolicyError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frameforge",
        description="Frame Forge - autonomous game operation and QA for Windows",
    )
    parser.add_argument("--config", type=Path, default=None, help="JSON settings file")
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="store_true")

    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="report installed capabilities honestly")
    doctor.add_argument("--probe-ocr", action="store_true", help="also measure OCR (slower)")

    run = sub.add_parser("run", help="run a scenario against a live target")
    run.add_argument("--task", type=Path, required=True)
    run.add_argument("--profile", type=Path, default=None, help="game profile YAML/JSON")
    run.add_argument("--monitor", type=int, default=None)
    run.add_argument("--launch", choices=["ui", "direct", "attach"], default=None)
    run.add_argument("--ai", action="store_true", help="enable the AI planner")
    run.add_argument("--dry-run", action="store_true",
                     help="wire everything but never send input")
    run.add_argument("--grace", type=float, default=None, help="seconds before first input")
    run.add_argument(
        "--refocus",
        action="store_true",
        help=(
            "allow Frame Forge to bring the target window to the foreground. Useful when "
            "running from a console, because launching a console application can take "
            "foreground on Windows. Off by default: taking focus away from the user is a "
            "courtesy, not an entitlement."
        ),
    )

    sim = sub.add_parser("simulate", help="run a scenario headless against fakes")
    sim.add_argument("--task", type=Path, required=True)
    sim.add_argument("--profile", type=Path, default=None)
    sim.add_argument("--frames", type=int, default=6, help="fake capture cycle length")

    sub.add_parser("profiles", help="list discovered profiles")
    sub.add_parser("validate", help="validate all profiles and tasks")

    inspect = sub.add_parser("inspect", help="summarise a run directory")
    inspect.add_argument("run_dir", type=Path)

    replay = sub.add_parser(
        "replay",
        help="re-run a recorded plan deterministically and diff it against the recording",
    )
    replay.add_argument("run_dir", type=Path, help="a run directory containing plan.jsonl")
    replay.add_argument("--task", type=Path, default=None,
                        help="scenario to replay through (defaults to the recorded run's)")
    replay.add_argument("--profile", type=Path, default=None)

    rec = sub.add_parser(
        "recover-input",
        help=(
            "repair keyboard/mouse/input-language state left behind by an interrupted run. "
            "Sends only key-up events; cannot type into a document or dismiss a dialog. "
            "Intended for when the keyboard is misbehaving and a restart is not "
            "acceptable."
        ),
    )
    rec.add_argument("--audit", action="store_true",
                     help="report current input state and change nothing")
    rec.add_argument("--locale", type=lambda v: int(v, 16), default=0x4009,
                     help="input locale to restore, as hex (default 0x4009 = en-US)")
    rec.add_argument("--log", type=Path, default=None, help="write a recovery log here")
    rec.add_argument("--dry-run", action="store_true", help="report state, change nothing")
    rec.add_argument("--layout", default="0409", help="preferred keyboard layout hex")
    rec.add_argument(
        "--persist-default",
        action="store_true",
        help=(
            "durable fix: change the Windows *user's* default input method so the layout "
            "change survives process exit. This alters a system-wide user setting and is "
            "never done automatically by a run."
        ),
    )
    rec.add_argument("--input-tip", default="0409:00000409",
                     help="input method tip for --persist-default (0409:00000409 = en-US)")

    arm = sub.add_parser(
        "arm",
        help=(
            "arm live input for a registered target. Requires --attest: the operator's own "
            "confirmation that they are in active gameplay. Without a registered target, a "
            "live capture, and that attestation, this refuses."
        ),
    )
    arm.add_argument("--task", type=Path, required=True)
    arm.add_argument("--profile", type=Path, required=True)
    arm.add_argument(
        "--attest", required=False, default="",
        help=("the operator's confirmation: 'I am in active gameplay and my character is "
              "controllable'. There is no default and no way to skip it."),
    )
    arm.add_argument("--countdown-ms", type=int, default=4000)
    arm.add_argument("--confirm", action="store_true",
                     help="complete the countdown and attempt to enter ACTIVE")

    status = sub.add_parser(
        "status",
        help="read-only: show run state, input lock, target identity and protected windows",
    )
    status.add_argument("--task", type=Path, default=None)
    status.add_argument("--profile", type=Path, default=None)

    serve = sub.add_parser("serve", help="run the local control API (dashboard backend)")
    serve.add_argument("--port", type=int, default=8756)
    serve.add_argument("--host", type=str, default="127.0.0.1")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from frameforge import __version__

        print(f"frameforge {__version__}")
        return 0

    if not args.command:
        parser.print_help()
        return 0

    try:
        overrides = {}
        if getattr(args, "runs_dir", None):
            overrides["runs_dir"] = args.runs_dir
        if getattr(args, "verbose", False):
            overrides["verbose"] = True
        settings = load_settings(args.config, **overrides)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    match args.command:
        case "doctor":
            return _cmd_doctor(args, settings)
        case "run":
            return _cmd_run(args, settings)
        case "simulate":
            return _cmd_simulate(args, settings)
        case "profiles":
            return _cmd_profiles(args, settings)
        case "validate":
            return _cmd_validate(args, settings)
        case "inspect":
            return _cmd_inspect(args)
        case "replay":
            return _cmd_replay(args, settings)
        case "recover-input":
            return _cmd_recover_input(args, settings)
        case "arm":
            return _cmd_arm(args, settings)
        case "status":
            return _cmd_status(args, settings)
        case "serve":
            return _cmd_serve(args, settings)
    parser.print_help()
    return 0


# ----------------------------------------------------------------------- doctor


def _cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.cli.doctor import format_checks, run_checks

    print(format_checks(run_checks(verbose=getattr(args, "probe_ocr", False) or settings.verbose)))
    return 0


# ------------------------------------------------------------------- profiles


def _cmd_profiles(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.profiles.loader import discover_profiles

    found = discover_profiles(settings.profiles_dir)
    if not found:
        print(f"no profiles found under {settings.profiles_dir}")
        return 0
    print(f"profiles under {settings.profiles_dir}:")
    for name, path in sorted(found.items()):
        print(f"  {name:24} {path}")
    return 0


def _cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.profiles.loader import discover_profiles, load_game_profile, load_templates
    from frameforge.tasks.loader import load_task

    failures = 0
    print("validating profiles...")
    for name, path in sorted(discover_profiles(settings.profiles_dir).items()):
        try:
            spec = load_game_profile(path)
            spec.metadata["_profile_dir"] = str(path.parent)
            try:
                load_templates(spec)
                template_note = "templates ok"
            except PolicyError as exc:
                template_note = f"TEMPLATES: {exc}"
                failures += 1
            print(f"  [ ok ] {name:24} target={spec.target.describe()} {template_note}")
            print(f"         authorised: {spec.authorized_use.describe()}")
        except Exception as exc:
            failures += 1
            print(f"  [BAD ] {name:24} {type(exc).__name__}: {exc}")

    print("validating tasks...")
    for path in sorted(settings.tasks_dir.rglob("*")):
        if path.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        try:
            scenario = load_task(path)
            print(f"  [ ok ] {path.name:28} steps={len(scenario.steps)} "
                  f"assertions={len(scenario.assertions)} mode={scenario.launch_mode}")
        except Exception as exc:
            failures += 1
            print(f"  [BAD ] {path.name:28} {type(exc).__name__}: {exc}")

    print()
    print(f"{'all valid' if failures == 0 else f'{failures} problem(s) found'}")
    return 0 if failures == 0 else 1


# ----------------------------------------------------------------------- inspect


def _cmd_inspect(args: argparse.Namespace) -> int:
    from frameforge.kernel.bus import load_events

    path = Path(args.run_dir)
    report = path / "report.json"
    if not report.exists():
        print(f"no report.json in {path}", file=sys.stderr)
        return 2
    data = json.loads(report.read_text(encoding="utf-8"))
    print(f"run       : {data.get('run_id')}")
    print(f"verdict   : {data.get('overall').upper()}  (state {data.get('state')})")
    print(f"scenario  : {data.get('scenario')}  build={data.get('build_id') or '-'}")
    print(f"launch    : {data.get('launch_mode')}  planner={data.get('planner')}")
    print(f"counts    : {data.get('counts')}")
    fd = data.get("first_divergence") or {}
    if fd.get("step"):
        print(f"divergence: step {fd['step']!r} expected={fd.get('expected')!r} actual={fd.get('actual')!r}")
    events = load_events(path / "events.jsonl")
    print(f"events    : {len(events)} recorded")
    counts: dict[str, int] = {}
    for e in events:
        counts[str(e.kind)] = counts.get(str(e.kind), 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    print("top kinds :")
    for kind, n in top:
        print(f"  {n:6}  {kind}")
    return 0


# ----------------------------------------------------------------------- replay


def _cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    """Re-run a recorded plan and diff it.

    This is the determinism proof: the same logical actions must produce the same plan.
    It exercises the real validator, compiler, budget, focus guard, executor and verifier -
    only the decision source is swapped for the recording.
    """
    import json as _json

    from frameforge.ports import fakes
    from frameforge.ports.capture import Surface, SurfaceKind
    from frameforge.ports.geometry import Size
    from frameforge.store.runs import RunPaths
    from frameforge.tasks.replay import RecordedPlanner, diff_plans, load_plan
    from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

    run_dir = Path(args.run_dir)
    paths = RunPaths(root=run_dir)
    try:
        recorded = load_plan(paths)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"recorded actions: {len(recorded)}")

    report_path = paths.report_json
    scenario_name = ""
    if report_path.exists():
        try:
            scenario_name = _json.loads(report_path.read_text(encoding="utf-8")).get("scenario", "")
        except Exception:
            pass

    task_path = args.task
    if task_path is None and scenario_name:
        for candidate in sorted(settings.tasks_dir.rglob(f"{scenario_name}.*")):
            if candidate.suffix.lower() in (".json", ".yaml", ".yml"):
                task_path = candidate
                break
    if task_path is None:
        print("error: no task found; pass --task", file=sys.stderr)
        return 2

    from frameforge.profiles.loader import load_game_profile
    from frameforge.tasks.loader import load_task

    profile = None
    if args.profile:
        profile = load_game_profile(args.profile)
        profile.metadata["_profile_dir"] = str(Path(args.profile).parent)
    scenario = load_task(task_path)

    surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000, label="replay")
    capture = fakes.FakeCapture(surface)
    capture.open(surface)
    # The fake must satisfy the profile's TargetSpec. Otherwise target resolution returns
    # None, the focus guard has no target, and every action is (correctly) blocked - which
    # looks like a replay failure rather than a wiring mistake.
    if profile is not None:
        window = fakes.FakeWindow(
            title=profile.display_name or profile.name,
            class_name=profile.target.class_name or "FakeTargetClass",
            process_name=profile.target.process_name or "fake.exe",
        )
    else:
        window = fakes.FakeWindow()
    # Replay compares *plans*, not verdicts, so text landmarks are replayed as visible to
    # let the actions execute. Verdict reproducibility against a live target is a different
    # question, answered by re-running the scenario against the real window.
    wiring = RunnerWiring(
        window=window, capture=capture,
        ocr=fakes.FakeOcr(default=_sample_text_for_landmarks(profile)),
        vision=fakes.FakeVision(), input_port=fakes.FakeInput(),
        profile=profile, surface=surface,
    )
    replay_settings = Settings(runs_dir=settings.runs_dir, grace_seconds=0.0)
    runner = ScenarioRunner(replay_settings, wiring, scenario=scenario)
    # The override must be in place before prepare(), which is where the director is wired.
    runner._override_planner = RecordedPlanner(recorded)
    runner.prepare()

    _outcome, report = runner.run()
    replayed = load_plan(runner.paths)
    diff = diff_plans(recorded, replayed)

    print()
    print(diff.describe())
    print()
    print(f"replay report: {runner.paths.report_md}")
    if diff.identical:
        print("REPLAY DETERMINISTIC: the recorded plan replayed exactly.")
        return 0
    print("REPLAY DRIFT: the recorded plan did not reproduce.")
    return 1


# ------------------------------------------------------------------- recover-input


def _cmd_recover_input(args: argparse.Namespace, settings: Settings) -> int:
    """Repair input state without restarting Windows.

    Sends no keystrokes to any window, so it cannot type into a document or dismiss a
    dialog - it only releases state and re-activates the intended keyboard layout.
    """
    from frameforge.actions.safety import (
        list_layouts,
        read_layout,
        recover_input_control,
        stuck_modifiers,
    )

    print("current input state")
    print(f"  layout      : {read_layout().describe()}")
    print(f"  loaded HKLs : {[f'0x{h:08X}' for h in list_layouts()]}")
    stuck = {k: v for k, v in stuck_modifiers().items() if v}
    print(f"  modifiers   : {stuck or 'none down'}")

    if args.audit:
        print("\n--audit: nothing was changed")
        return 0

    log = args.log or (Path(settings.runs_dir) / "input_recovery.json")
    result = recover_input_control(prefer_locale=args.locale, log_path=log)

    print("\nrecovery")
    print(f"  layout      : {result['before_layout']} -> {result['after_layout']}")
    print(f"  intended    : {result.get('intended_layout')}")
    print(f"  modifiers   : {result.get('stuck_modifiers_after') or 'none down'}")
    print(f"  result      : {result['result']}")
    healthy = bool(result.get("healthy"))
    print(f"\n{'INPUT STATE IS CLEAN' if healthy else 'INPUT STATE STILL DEGRADED'}")
    return 0 if healthy else 1


# --------------------------------------------------------------- input recovery


# --------------------------------------------------------------------- live input


def _prepare_runner(args: argparse.Namespace, settings: Settings, *, need_task: bool = True):
    """Build a real ScenarioRunner through the normal path, for status and arm alike.

    Deliberately the same construction ``run`` uses. A separate "arm" code path would be a
    second way to reach the input backend, and two ways is how bypasses happen.
    """
    from frameforge.adapters.capture.registry import build_capture_source
    from frameforge.adapters.ocr.rapidocr_ocr import build_ocr
    from frameforge.adapters.vision.opencv_vision import OpenCvVision
    from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
    from frameforge.profiles.loader import load_game_profile, load_templates
    from frameforge.tasks.loader import load_task
    from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

    if not args.task or not args.profile:
        print("error: --task and --profile are both required", file=sys.stderr)
        return None

    scenario = load_task(args.task)
    profile = load_game_profile(args.profile)
    profile.metadata["_profile_dir"] = str(Path(args.profile).parent)
    load_templates(profile)

    window = PyWin32WindowAdapter()
    info = window.find_window(profile.target)
    if info is None:
        from frameforge.kernel.errors import AmbiguousTargetError

        try:
            window.find_window(profile.target)
        except AmbiguousTargetError as exc:
            print(f"error: {exc}", file=sys.stderr)
        else:
            print(f"error: target window not found ({profile.target.describe()}).",
                  file=sys.stderr)
        return None

    surface = None
    if info.client_rect is not None:
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size

        surface = Surface(
            kind=SurfaceKind.WINDOW, size=info.client_rect.size,
            offset_x=info.client_origin_screen.x, offset_y=info.client_origin_screen.y,
            hwnd=info.hwnd, label=info.title,
        )
        capture = build_capture_source(settings.capture_backend, window, surface)
    else:
        from frameforge.ports.capture import Surface, SurfaceKind
        from frameforge.ports.geometry import Size

        surface = Surface(kind=SurfaceKind.MONITOR, size=Size(1920, 1080), hwnd=info.hwnd)
        capture = build_capture_source(settings.capture_backend, window, surface)

    ocr = build_ocr(settings.ocr_backend)
    wiring = RunnerWiring(
        window=window, capture=capture, ocr=ocr,
        vision=OpenCvVision(),
        input_port=_DryRunPort(),          # status and arm never inject
        profile=profile, templates={}, surface=surface,
    )
    runner = ScenarioRunner(settings, wiring, scenario=scenario)
    runner.prepare()
    # One observation, so a capture exists to bind to the session.
    runner.fetch_observation()
    return runner


class _DryRunPort:
    """A port that can never inject. Used by status/arm so those paths cannot shoot."""

    name = "sendinput"

    def __init__(self):
        from frameforge.actions.safety import InputSafetyManager

        self.safety = InputSafetyManager()
        self._enabled = False
        self.sent: list = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool, thorough: bool = True) -> None:
        self._enabled = value

    def send(self, primitive, action_id: str = "", source: str = "") -> bool:
        self.sent.append(primitive)
        return True

    def send_batch(self, prims, action_id: str = "") -> int:
        for prim in prims:
            self.send(prim, action_id)
        return len(prims)

    def release_all(self, thorough: bool = True) -> dict:
        return self.safety.release_all(thorough=thorough)

    def position(self):
        from frameforge.ports.geometry import Point

        return Point(0, 0)


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    """Read-only. Never injects, never focuses, never changes state."""
    if not args.profile:
        print("INPUT LOCKED")
        print("  no profile supplied; nothing is registered")
        print("  live input requires: --profile <target> and an explicit arm")
        return 0
    runner = _prepare_runner(args, settings)
    if runner is None:
        return 2
    status = runner.live_input_status()
    print(runner.machine.banner())
    print()
    print(f"  state          : {status['state']}")
    print(f"  input locked   : {status['input_locked']}")
    session = status.get("session") or {}
    if session:
        print(f"  target hwnd    : {session.get('hwnd')}")
        print(f"  target pid     : {session.get('pid')}")
        print(f"  executable     : {session.get('image_path')}")
        print(f"  window         : {session.get('class_name')} {session.get('title')!r}")
        print(f"  client rect    : {session.get('client_rect')}")
        print(f"  monitor / dpi  : {session.get('monitor_index')} {session.get('monitor_device')} dpi={session.get('dpi')}")
    protected = status.get("protected") or {}
    if protected:
        print(f"  protected pids : {protected.get('protected_pids')}")
        print(f"  protected wins : {len(protected.get('protected_hwnds', []))} hwnd(s)")
        print(f"  protected class: {protected.get('protected_classes')}")
    try:
        runner.wiring.input_port.release_all()
    except Exception:
        pass
    return 0


def _cmd_arm(args: argparse.Namespace, settings: Settings) -> int:
    """The only CLI route to live input, and it refuses unless fully authorised."""
    if not args.attest.strip():
        print("error: --attest is required.", file=sys.stderr)
        print("       There is no default. Confirm explicitly:", file=sys.stderr)
        print('       --attest "I am in active gameplay and my character is controllable"',
              file=sys.stderr)
        print("       Frame Forge never infers this state.", file=sys.stderr)
        return 2

    runner = _prepare_runner(args, settings)
    if runner is None:
        return 2

    result = runner.arm_for_live_input(
        attestation=args.attest, countdown_ms=float(args.countdown_ms))
    if result.blocked:
        print(f"ARM REFUSED: {result.detail}", file=sys.stderr)
        try:
            runner.wiring.input_port.release_all()
        except Exception:
            pass
        return 3

    print(f"armed profile={runner.machine.arm_token.profile_name!r}")
    print(f"state: {runner.machine.state.value}")
    print(f"countdown {args.countdown_ms / 1000:.0f}s — return focus to the target now.")

    if not args.confirm:
        print("\nnot confirmed; re-run with --confirm after the countdown.")
        print(runner.machine.banner())
        return 0

    print("\nwaiting for the countdown...")
    import time as _t

    deadline = _t.monotonic() + (args.countdown_ms / 1000.0) + 1.0
    while _t.monotonic() < deadline:
        _t.sleep(0.2)
        try:
            runner.wiring.input_port.release_all()
        except Exception:
            pass

    final = runner.confirm_live_input()
    if final.blocked:
        print(f"NOT ACTIVE: {final.detail}", file=sys.stderr)
        print(f"state: {runner.machine.state.value}", file=sys.stderr)
        try:
            runner.wiring.input_port.release_all()
        except Exception:
            pass
        return 3

    print()
    print(runner.machine.banner())
    print("Live input is now permitted for the registered target only.")
    return 0


# -------------------------------------------------------------------------- serve


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.api.server import serve
    from frameforge.kernel.errors import FrameForgeError

    try:
        serve(settings.runs_dir, host=args.host, port=args.port)
    except FrameForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


# --------------------------------------------------------------------- simulate


def _cmd_simulate(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.ports import fakes
    from frameforge.ports.capture import Surface, SurfaceKind
    from frameforge.ports.geometry import Size
    from frameforge.profiles.loader import load_game_profile
    from frameforge.tasks.loader import load_task
    from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

    scenario = load_task(args.task)
    profile = None
    templates: dict = {}
    if args.profile:
        profile = load_game_profile(args.profile)
        profile.metadata["_profile_dir"] = str(Path(args.profile).parent)
        from frameforge.profiles.loader import load_templates as load_tpl

        templates = load_tpl(profile)

    surface = Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000, label="fake")
    capture = fakes.FakeCapture(surface)
    # The fake must satisfy the profile's TargetSpec, or target resolution fails and every
    # observation is None. A fake that does not match the profile under test is a fake that
    # silently tests nothing.
    if profile is not None:
        title = profile.display_name or profile.name
        class_name = profile.target.class_name or "FakeTargetClass"
        process_name = profile.target.process_name or "faketarget.exe"
        window = fakes.FakeWindow(title=title, class_name=class_name, process_name=process_name)
    else:
        window = fakes.FakeWindow()
    capture.open(surface)
    # Feed the fake OCR with sample text derived from the profile's own text landmarks.
    #
    # IMPORTANT AND DELIBERATE: `simulate` proves *plumbing*, not perception. It shows the
    # director, validator, compiler, executor, verifier and report builder are wired
    # correctly and produce a valid, deterministic run directory with no display attached.
    # It does NOT show that Frame Forge can read a screen - that requires the live path
    # against the testbed (`frameforge run`), because a fake that emits the exact strings a
    # profile declares would "prove" perception while testing nothing.
    ocr_lines = _sample_text_for_landmarks(profile)
    ocr = fakes.FakeOcr(default=ocr_lines)
    print("note: simulate exercises plumbing only - perception is proven by 'frameforge run'")
    print(f"      against a real window. Fake OCR is emitting {len(ocr_lines)} profile-declared lines.")

    wiring = RunnerWiring(
        window=window,
        capture=capture,
        ocr=ocr,
        vision=fakes.FakeVision(),
        input_port=fakes.FakeInput(),
        profile=profile,
        templates=templates,
        surface=surface,
    )
    runner = ScenarioRunner(settings, wiring, scenario=scenario)
    runner.prepare()
    outcome, report = runner.run()

    print(f"run       : {runner.run_id}")
    print(f"verdict   : {report.overall.upper()}  (state {report.state})")
    print(f"counts    : {report.counts}")
    print(f"actions   : {settings.max_actions and ''}see plan trace")
    if report.first_divergence.step:
        print(f"divergence: {report.first_divergence.step!r} "
              f"expected={report.first_divergence.expected!r} actual={report.first_divergence.actual!r}")
    print(f"report    : {runner.paths.report_json}")
    return 0 if report.overall in ("pass", "unknown") else 1


# -------------------------------------------------------------------------- run


def _safe_find(window, spec) -> int | None:
    """Resolve a target, treating ambiguity as "not up yet" rather than an error.

    During a launch poll, several windows may briefly match; the poll should keep waiting
    rather than abort, so ambiguity collapses to ``None`` here. A genuine ambiguity is
    still reported later, by the strict call site.
    """
    from frameforge.kernel.errors import AmbiguousTargetError

    try:
        info = window.find_window(spec)
    except AmbiguousTargetError:
        return None
    return info.hwnd if info else None


def _sample_text_for_landmarks(profile) -> list[str]:
    """Derive plausible sample strings from a profile's text landmark patterns.

    Strips anchors and expands the common regex escapes, so the generated text actually
    matches the pattern it came from and the assertion can genuinely pass. Anything more
    clever would be pretending to test perception.
    """
    import re

    out: list[str] = []
    if profile is None:
        return out
    for landmark in profile.landmarks:
        if landmark.kind != "text" or not landmark.pattern:
            continue
        sample = landmark.pattern
        sample = re.sub(r"\^|\$", "", sample)
        sample = re.sub(r"\\d\+?", "0", sample)
        sample = re.sub(r"\[dswDSW]", "x", sample)
        sample = re.sub(r"\(\?:", "(", sample)
        sample = re.sub(r"\(\.[^)]*\)", "x", sample)
        sample = sample.replace(".*", "x").replace(".+", "x")
        out.append(sample.strip())
    return out


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    from frameforge.adapters.capture.registry import build_capture_source
    from frameforge.adapters.input.sendinput import SendInputPort
    from frameforge.adapters.ocr.rapidocr_ocr import build_ocr
    from frameforge.adapters.vision.opencv_vision import OpenCvVision
    from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter
    from frameforge.ports.capture import Surface, SurfaceKind
    from frameforge.ports.geometry import Size
    from frameforge.profiles.loader import load_game_profile, load_templates
    from frameforge.tasks.loader import load_task
    from frameforge.tasks.runner import RunnerWiring, ScenarioRunner

    if args.monitor is not None:
        settings.monitor_index = args.monitor
    if args.launch is not None:
        settings.launch_mode = args.launch
        if args.launch == "direct":
            settings.allow_direct_launch = True
    if args.ai:
        settings.ai_enabled = True
    if args.grace is not None:
        settings.grace_seconds = args.grace
    if getattr(args, "refocus", False):
        settings.focus_policy = "refocus"
    settings.validate()

    scenario = load_task(args.task)
    if not args.profile:
        print(
            "error: --profile is required for a live run.\n"
            "  A profile declares the target window and the owner's authorised_use "
            "attestation; Frame Forge refuses to attach to an unidentified window "
            "(docs/AI_GUARDRAILS.md G-AUTH-01).",
            file=sys.stderr,
        )
        return 2

    profile = load_game_profile(args.profile)
    profile.metadata["_profile_dir"] = str(Path(args.profile).parent)
    templates = load_templates(profile)

    print(f"target    : {profile.target.describe()}")
    print(f"authorised: {profile.authorized_use.describe()}")
    print(f"scenario  : {scenario.name}  ({len(scenario.steps)} steps, "
          f"{len(scenario.assertions)} assertions)")
    print(f"launch    : {scenario.launch_mode}")
    if scenario.launch_mode == "direct" and not scenario.allow_direct_launch:
        print("error: scenario uses launch_mode=direct but does not set allow_direct_launch=true",
              file=sys.stderr)
        return 2
    print()

    window = PyWin32WindowAdapter()
    topo = window.monitors()
    monitor = topo.monitor(settings.monitor_index) or topo.monitor(topo.primary_index)
    assert monitor is not None

    # A scenario may declare how to start its own target. Opt-in per scenario, and the
    # exact command line goes into the report (G-BLAST-02).
    if scenario.launch is not None and scenario.allow_direct_launch:
        print(f"launch    : {scenario.launch.describe()}  (direct launch; recorded in report)")
        print(f"rationale : {scenario.launch.rationale or '(none given)'}")

    from frameforge.kernel.errors import AmbiguousTargetError
    from frameforge.tasks.launch import ensure_target, is_authorised, wait_for_landmark

    # Bring the target up before resolving it - the capture surface cannot be built until
    # there is a window to point at. Both calls go through the shared helper so the
    # authorisation policy lives in exactly one place.
    launched_pid = None
    if is_authorised(scenario):
        hwnd = ensure_target(scenario.launch, lambda: _safe_find(window, profile.target))
        if hwnd is not None:
            print(f"launched  : {scenario.launch.describe()}  (pid recorded in the report)")

    try:
        info = window.find_window(profile.target)
    except AmbiguousTargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"       profile: {profile.name!r}  spec: {profile.target.describe()}", file=sys.stderr)
        return 3
    if info is None:
        print(f"error: target window not found ({profile.target.describe()}).", file=sys.stderr)
        print("       Launch the application first, then re-run in attach mode.", file=sys.stderr)
        return 3
    print(f"acquired  : {info.describe()}")
    client = info.client_rect.size if info.client_rect else Size(monitor.width, monitor.height)
    surface = Surface(
        kind=SurfaceKind.WINDOW if settings.capture_scope == "window" else SurfaceKind.MONITOR,
        size=client,
        offset_x=info.client_origin_screen.x,
        offset_y=info.client_origin_screen.y,
        monitor_index=settings.monitor_index,
        hwnd=info.hwnd,
        label=info.title,
    )
    if settings.focus_policy == "refocus":
        print("focus     : Frame Forge may bring the target forward (--refocus)")
    if settings.grace_seconds > 0:
        print(f"grace     : {settings.grace_seconds:.0f}s before any input (move the mouse to cancel)")
        import time

        time.sleep(settings.grace_seconds)
        # A console-launched process can hold foreground, which would make the focus guard
        # refuse every input. Under --refocus, claim the target's foreground *before* the
        # run starts rather than discovering the problem mid-scenario.
        if settings.focus_policy == "refocus":
            window.set_foreground(info.hwnd)

    capture = build_capture_source(
        settings.capture_backend, window, surface,
        on_warning=lambda m: print(f"warn: {m}", file=sys.stderr),
    )
    ocr = build_ocr(settings.ocr_backend)
    input_port = SendInputPort(dry_run=args.dry_run)
    if args.dry_run:
        print("dry-run   : input will NOT be sent to the OS")

    wiring = RunnerWiring(
        window=window,
        capture=capture,
        ocr=ocr,
        vision=OpenCvVision(),
        input_port=input_port,
        profile=profile,
        templates=templates,
        surface=surface,
    )
    runner = ScenarioRunner(settings, wiring, scenario=scenario)
    runner._prelaunched = launched_pid is not None or scenario.launch is None
    runner.prepare()

    # A window existing is not the same as the application being ready to be driven.
    if is_authorised(scenario) and scenario.launch.ready_landmark:
        print(f"waiting   : for readiness landmark {scenario.launch.ready_landmark!r}")
        ready = wait_for_landmark(
            scenario.launch.ready_landmark,
            runner.fetch_observation,
            clock=runner.clock,
            events=runner.events,
            timeout_ms=scenario.launch.timeout_ms,
        )
        if not ready:
            print("error: the application started but never became ready.", file=sys.stderr)
            capture.close()
            return 4

    outcome, report = runner.run()
    capture.close()
    try:
        ocr.close()
    except Exception:
        pass

    print()
    print(f"verdict   : {report.overall.upper()}")
    print(f"counts    : {report.counts}")
    for rec in report.failure_digest[:10]:
        print(f"  FAIL {rec.name}: expected={rec.expected!r} actual={rec.actual!r}")
    print(f"report    : {runner.paths.report_md}")
    print(f"events    : {runner.paths.events}")
    return 0 if report.overall in ("pass", "unknown") else 1


if __name__ == "__main__":
    raise SystemExit(main())
