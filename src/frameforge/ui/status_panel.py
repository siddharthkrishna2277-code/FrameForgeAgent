"""Read-only diagnostic panel.

Read-only is a structural property, not a policy: this module builds a Tk tree from a plain
snapshot dict and holds no reference to any mutating runner method. There is no ARM button
here, and adding one would require deliberately reaching past this boundary into the
execution path.

Deliberately absent until the CLI-driven live POC succeeds on a real desktop: ARM, target
selection, countdown, pause/resume, gameplay confirmation, emergency stop. Those controls
would be operating a backend this build has only ever exercised against fakes.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any


# Status labels carry their verification scope, because "verified" without a scope is how a
# reader comes to believe a mock-path check was a live-desktop check.
SCOPE_MOCK = "COMPLETE_AND_VERIFIED_MOCK"
SCOPE_LIVE_ISOLATED = "COMPLETE_AND_VERIFIED_LIVE_ISOLATED"
SCOPE_LIVE_DESKTOP = "COMPLETE_AND_VERIFIED_LIVE_USER_DESKTOP"
SCOPE_NOT_VERIFIED = "IMPLEMENTED_NOT_VERIFIED"
SCOPE_PARTIAL = "PARTIAL"
SCOPE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"

SCOPE_NOTE = (
    "MOCK = verified through the real runner against a recording input backend.\n"
    "LIVE_ISOLATED = verified on a real desktop in an isolated test session.\n"
    "LIVE_USER_DESKTOP = verified on the operator's own desktop with live input."
)


@dataclass(frozen=True)
class GateRow:
    name: str
    scope: str
    evidence: str


@dataclass(frozen=True)
class Snapshot:
    """Everything the panel shows. A value type, so the panel cannot act on anything."""

    state: str = "unknown"
    input_locked: bool = True
    input_lock_reason: str = "no session"
    target_identity: dict[str, Any] = field(default_factory=dict)
    session_expiry: str = "n/a"
    last_gate_decision: dict[str, Any] = field(default_factory=dict)
    protected_windows: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    emergency_stop: dict[str, Any] = field(default_factory=dict)
    capture_health: dict[str, Any] = field(default_factory=dict)
    display_topology: dict[str, Any] = field(default_factory=dict)
    gates: list[GateRow] = field(default_factory=list)


#: The gate register. Scope is stated per row; never collapse these into a bare "verified".
GATE_REGISTER: tuple[GateRow, ...] = (
    GateRow("P0  no locationless click", SCOPE_MOCK,
            "Click(at=None) rejected at schema, compiler and runtime; no MOUSE_MOVE emitted"),
    GateRow("P0.5 no fixed screen point", SCOPE_MOCK,
            "static AST guard: zero hard-coded absolute points in tests/, scripts/, fixtures/"),
    GateRow("P1  target session mandatory", SCOPE_MOCK,
            "run with no --profile has no session; arming refuses"),
    GateRow("P2  WindowFromPoint", SCOPE_MOCK,
            "checked before movement and again before button-down"),
    GateRow("P3  protected registry", SCOPE_MOCK,
            "by pid/hwnd; this process plus 7 shell classes always denied"),
    GateRow("P4  DPI awareness", SCOPE_MOCK,
            "per_monitor_v2 verified in force on this machine; dpi/scale in fingerprint"),
    GateRow("P5  topology on dispatch", SCOPE_MOCK,
            "re-read before every mouse action; disconnect/reorder/scale invalidates"),
    GateRow("P6  target-scoped capture", SCOPE_MOCK,
            "frame bound to hwnd/pid/session; stale/black/mismatched refuses"),
    GateRow("P7  UI Automation", SCOPE_NOT_IMPLEMENTED,
            "no UIA backend; visual verification is OCR-only and recall-limited"),
    GateRow("P8  user-armed state machine", SCOPE_MOCK,
            "ACTIVE unreachable without token + session + countdown"),
    GateRow("P9  emergency stop", SCOPE_MOCK,
            "3 paths, 16 ms steady state, full defensive release"),
)


class StatusPanel:
    """A read-only view. Renders a snapshot; holds nothing that can change state."""

    def __init__(self, snapshot: Snapshot, *, title: str = "Frame Forge - status (read-only)") -> None:
        self.snapshot = snapshot
        self.root = tk.Tk()
        self.root.title(title)
        self.root.attributes("-topmost", False)   # never steal focus from the operator
        self.root.configure(bg="#12141a")
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        root = self.root
        root.geometry("1040x760")

        header = tk.Frame(root, bg="#12141a")
        header.pack(fill="x", padx=14, pady=(12, 6))
        self._banner = tk.Label(
            header, text="", bg="#12141a", fg="#f0b429",
            font=("Segoe UI", 11, "bold"), anchor="w", justify="left")
        self._banner.pack(fill="x")

        body = tk.Frame(root, bg="#12141a")
        body.pack(fill="both", expand=True, padx=14, pady=6)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        self._state = self._section(left, "Execution state")
        self._lock = self._section(left, "Input authority")
        self._target = self._section(left, "Registered target")
        self._session = self._section(left, "Session and capture")
        self._decision = self._section(left, "Last gate decision")

        right = ttk.Frame(body)
        right.pack(side="right", fill="both", expand=True, padx=(10, 0))
        self._gates = self._section(right, "Safety gates (scope stated)")
        self._protected = self._section(right, "Protected windows")
        self._estop = self._section(right, "Emergency stop")

        foot = tk.Frame(root, bg="#12141a")
        foot.pack(fill="both", expand=True, padx=14, pady=(4, 10))
        self._audit = self._section(foot, "Audit trail", height=9)

        note = tk.Label(
            root, text=SCOPE_NOTE, bg="#12141a", fg="#6b7280",
            font=("Segoe UI", 8), anchor="w", justify="left")
        note.pack(fill="x", padx=14, pady=(0, 8))

        # No control buttons. An ARM button here would need a live reference to the
        # execution path, which this module deliberately does not hold.

    def _section(self, parent, title: str, height: int = 0):
        frame = tk.Frame(parent, bg="#1a1d26", highlightbackground="#2a2f3d",
                         highlightthickness=1)
        frame.pack(fill="x", pady=4)
        tk.Label(frame, text=title.upper(), bg="#1a1d26", fg="#8b93a7",
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        text = tk.Text(frame, bg="#1a1d26", fg="#d7dbe4", relief="flat", height=height or 5,
                       font=("Consolas", 9), wrap="none", padx=8, pady=4)
        if height:
            text.pack(fill="both", expand=True, padx=2, pady=2)
        else:
            text.pack(fill="x", padx=2, pady=2)
        text.configure(state="disabled")
        return text

    # ------------------------------------------------------------------ render

    def refresh(self, snapshot: Snapshot | None = None) -> None:
        if snapshot is not None:
            self.snapshot = snapshot
        s = self.snapshot
        live = not s.input_locked

        # The banner is the first thing read. Say plainly that this is not a live surface.
        self._banner.configure(
            text=("READ-ONLY DIAGNOSTIC PANEL  -  no ARM control  -  "
                  + ("INPUT UNLOCKED (live)" if live else "INPUT LOCKED")))

        self._put(self._state, [
            ("state", s.state),
            ("target registered", bool(s.target_identity)),
            ("input locked", s.input_locked),
        ])
        self._put(self._lock, [
            ("reason", s.input_lock_reason),
            ("single authority", "LiveGate (actions/runtime.py)"),
            ("arm token", "required; no default"),
        ])
        self._put(self._target, [
            (k, v) for k, v in s.target_identity.items()] or [("none", "no target registered")])
        self._put(self._session, [
            ("expires", s.session_expiry),
        ] + [(k, v) for k, v in s.capture_health.items() if k != "frame"])
        self._put(self._decision, [
            (k, v) for k, v in s.last_gate_decision.items()] or [("none", "no decision yet")])
        self._put(self._gates, [(g.name, f"{g.scope}  ({g.evidence})") for g in s.gates])
        self._put(self._protected, [
            (p.get("label", "?"), p.get("reason", "protected")) for p in s.protected_windows]
            or [("none", "registry empty")])
        self._put(self._estop, [(k, v) for k, v in s.emergency_stop.items()] or [("state", "armed")])
        self._put(self._audit, [
            (e.get("t", ""), f"{e.get('event','')} {e.get('detail','')}") for e in s.audit_events]
            or [("none", "no events recorded")])

    def _put(self, widget: tk.Text, rows) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for k, v in rows:
            widget.insert("end", f"{k:<22} {v}\n")
        widget.configure(state="disabled")

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> None:      # pragma: no cover - requires a display
        self.root.mainloop()

    def close(self) -> None:    # pragma: no cover
        self.root.destroy()


def snapshot_from_runner(runner) -> Snapshot:
    """Build a snapshot from a live runner. Reads only.

    Every field is a read of public state. Nothing here can authorise or dispatch input.
    """
    status = runner.status() if hasattr(runner, "status") else {}
    machine_state = getattr(getattr(runner, "machine", None), "state", None)
    return Snapshot(
        state=getattr(machine_state, "value", str(machine_state or "unknown")),
        input_locked=status.get("input_locked", True),
        input_lock_reason=status.get("input_lock_reason", "unknown"),
        target_identity=status.get("target_identity", {}) or {},
        session_expiry=status.get("session_expiry", "n/a"),
        last_gate_decision=status.get("last_gate_decision", {}) or {},
        protected_windows=status.get("protected_windows", []) or [],
        audit_events=status.get("audit_events", []) or [],
        emergency_stop=status.get("emergency_stop", {}) or {},
        capture_health=runner.capture_health() if hasattr(runner, "capture_health") else {},
        display_topology=status.get("display_topology", {}) or {},
        gates=list(GATE_REGISTER),
    )


def main() -> int:      # pragma: no cover - requires a display
    """Show a static, read-only snapshot of this machine. Injects nothing."""
    from frameforge.adapters.window.pywin32_window import (
        PyWin32WindowAdapter, dpi_awareness, monitor_topology_fingerprint)

    monitors = []
    for m in PyWin32WindowAdapter().monitors_detailed():
        monitors.append({
            "device": m.device_name,
            "rect": m.rect.as_tuple(),
            "dpi": m.dpi,
            "scale": f"{m.scale_percent}%",
            "primary": m.is_primary,
        })

    snap = Snapshot(
        state="idle (no run)",
        input_locked=True,
        input_lock_reason="no run in progress",
        display_topology={
            "dpi mode": dpi_awareness().get("mode"),
            "fingerprint": (monitor_topology_fingerprint() or "")[:72] + "...",
            "monitors": monitors,
        },
        protected_windows=[
            {"label": "this process", "reason": "self-protection, always denied"},
            {"label": "Windows shell", "reason": "taskbar / start / shell classes"},
        ],
        emergency_stop={"state": "armed", "paths": 3},
        gates=list(GATE_REGISTER),
    )
    StatusPanel(snap).run()
    return 0


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(main())
