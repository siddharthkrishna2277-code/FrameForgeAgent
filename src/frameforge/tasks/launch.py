"""Bringing a target up, with the authorisation and reporting that must accompany it.

Extracted from the runner because two callers need it at different moments: the CLI needs
the window resolved *before* it can build a capture surface, while the runner needs it
before observation. Rather than duplicate the policy, both call :func:`ensure_target`.

Authorisation (guardrail G-BLAST-02):

* direct launch is opt-in per scenario (``allow_direct_launch``); no flag, env var or
  profile can enable it on its own;
* the executable is an argv list, never a shell string;
* the exact command line and the launching pid are recorded in the report, so a reader can
  always tell a launched run from an attached one.

Readiness is a declared landmark rather than a sleep: a fixed delay either wastes time or
races the application's own startup, and both produce flaky runs that read as product
failures.
"""

from __future__ import annotations

from typing import Any

from frameforge.kernel.bus import EventLog
from frameforge.kernel.clock import ClockPort, SystemClock
from frameforge.kernel.events import EventKind
from frameforge.actions.safety import hardened_child_env
from frameforge.tasks.dsl import LaunchSpec


def is_authorised(scenario: Any) -> bool:
    """Whether this scenario may start its own target."""
    spec = getattr(scenario, "launch", None)
    if spec is None:
        return False
    return bool(getattr(scenario, "allow_direct_launch", False))


def ensure_target(
    spec: LaunchSpec,
    resolve,
    clock: ClockPort | None = None,
    events: EventLog | None = None,
    *,
    timeout_ms: int | None = None,
    poll_ms: int = 300,
    before_launch=None,
) -> int | None:
    """Ensure the target window is up. Returns its hwnd, or ``None`` if it never appeared.

    ``resolve`` is a callable returning an hwnd (or ``None``); it is injected so this
    module does not depend on the window adapter and stays testable headlessly.
    """
    clock = clock or SystemClock()

    if resolve() is not None:
        return resolve()

    if before_launch is not None:
        try:
            before_launch()
        except Exception:
            pass

    import subprocess

    argv = spec.argv()
    if events is not None:
        events.append(
            EventKind.POLICY_CHECKED, "launch",
            f"direct launch authorised: {spec.describe()}",
            executable=spec.executable, args=list(spec.args),
            rationale=spec.rationale, launch_mode="direct", argv=argv,
        )
    try:
        # argv list, shell=False: nothing is ever concatenated into a command line.
        proc = subprocess.Popen(  # noqa: S603
            argv,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            shell=False,
            # Injection-capable variables are stripped from every child we spawn.
            env=hardened_child_env(),
        )
    except Exception as exc:
        if events is not None:
            events.append(
                EventKind.WINDOW_LOST, "launch",
                f"launch failed: {type(exc).__name__}: {exc}", error=str(exc), argv=argv,
            )
        return None

    limit = timeout_ms if timeout_ms is not None else spec.timeout_ms
    deadline = clock.monotonic_ms() + limit
    while clock.monotonic_ms() < deadline:
        hwnd = resolve()
        if hwnd is not None:
            if events is not None:
                events.append(
                    EventKind.WINDOW_FOUND, "launch",
                    f"launched and target window appeared: {spec.describe()}",
                    pid=proc.pid, hwnd=hwnd, launch_mode="direct", argv=argv,
                )
            return hwnd
        clock.sleep_ms(poll_ms)

    if events is not None:
        events.append(
            EventKind.WINDOW_LOST, "launch",
            f"launched but the target window did not appear within {limit}ms",
            pid=proc.pid, argv=argv,
        )
    return None


def wait_for_landmark(
    ready_landmark: str,
    observe,
    clock: ClockPort | None = None,
    events: EventLog | None = None,
    *,
    timeout_ms: int = 20_000,
    poll_ms: int = 250,
) -> bool:
    """Poll until the readiness landmark is visible.

    Separate from :func:`ensure_target` because it needs a fully wired perception stack,
    which only exists after the runner has been prepared. A window existing is not the same
    as an application being ready.
    """
    if not ready_landmark:
        return True
    clock = clock or SystemClock()
    deadline = clock.monotonic_ms() + timeout_ms
    errors: list[str] = []
    while clock.monotonic_ms() < deadline:
        try:
            observation = observe()
        except Exception as exc:
            # Recorded, not swallowed. A blanket except here hid a real defect (a missing
            # Win32 symbol) behind a generic "never became ready", which cost an entire
            # debugging cycle. A perception error is information, not a reason to go quiet.
            message = f"{type(exc).__name__}: {exc}"
            if message not in errors:
                errors.append(message)
                if events is not None:
                    events.append(
                        EventKind.PERCEPTION_DEGRADED, "launch",
                        f"readiness poll raised {message}", error=message,
                    )
            observation = None
        if observation is not None and observation.anchor_present(ready_landmark):
            return True
        clock.sleep_ms(poll_ms)
    if events is not None:
        events.append(
            EventKind.WINDOW_FOUND, "launch",
            f"target window is up but readiness landmark {ready_landmark!r} never appeared",
            landmark=ready_landmark, perception_errors=errors[:5],
        )
    return False


__all__ = ["ensure_target", "is_authorised", "wait_for_landmark"]
