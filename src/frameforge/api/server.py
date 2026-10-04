"""FastAPI control surface for the future dashboard and remote executor.

Endpoints are deliberately thin. Every one delegates to a service, so the dashboard and a
future remote Controller would be two clients of one backend rather than two
implementations of it.

Security posture, stated plainly: **loopback bind only, no authentication.** That is
acceptable for a dashboard on the same machine and unacceptable on a network. The bind
address is validated rather than documented, because a security property that only exists
in a comment is not a property.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frameforge.kernel.errors import FrameForgeError
from frameforge.store.runs import RunPaths, latest_run


@dataclass
class RunRegistry:
    """Tracks in-flight and completed runs for the API to report on.

    Kept in-process and deliberately simple. The durable record is the run directory; this
    is only a live view, so there is nothing here worth persisting and no way for it to
    become a second source of truth.
    """

    _runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, run_id: str, info: dict[str, Any]) -> None:
        with self._lock:
            self._runs[run_id] = info

    def update(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].update(changes)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._runs[run_id]) if run_id in self._runs else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._runs.values()]

    def stop(self, run_id: str) -> bool:
        """Request a stop via the run's ABORT sentinel file.

        Uses the sentinel rather than an in-memory flag because it works across processes:
        a dashboard, a CLI, or a script can all stop a run by touching a file.
        """
        info = self.get(run_id)
        if not info:
            return False
        sentinel = info.get("sentinel")
        if not sentinel:
            return False
        Path(sentinel).parent.mkdir(parents=True, exist_ok=True)
        Path(sentinel).write_text("abort", encoding="utf-8")
        self.update(run_id, stop_requested=True)
        return True


def create_app(runs_dir: Path, registry: RunRegistry | None = None):
    """Build the FastAPI application.

    Imported lazily so the core dependency floor stays small: a user who never runs the API
    should not need fastapi installed at all.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover
        msg = (
            "the control API needs FastAPI. Install the optional extra: "
            "pip install -e '.[api]'"
        )
        raise FrameForgeError(msg) from exc

    registry = registry or RunRegistry()
    app = FastAPI(
        title="Frame Forge control API",
        version="0.1.0",
        description=(
            "Local control surface for Frame Forge. Loopback only, no authentication: "
            "the dashboard is expected to run on the same machine as the executor."
        ),
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        from frameforge import __version__

        return {"ok": True, "version": __version__}

    @app.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        """Live runs first, then whatever is on disk."""
        live = registry.all()
        known = {r.get("run_id") for r in live}
        on_disk: list[dict[str, Any]] = []
        root = Path(runs_dir)
        if root.exists():
            for path in sorted(root.glob("ff-*"), reverse=True)[:50]:
                if path.name in known:
                    continue
                report = path / "report.json"
                entry: dict[str, Any] = {"run_id": path.name, "state": "recorded"}
                if report.exists():
                    try:
                        import json

                        data = json.loads(report.read_text(encoding="utf-8"))
                        entry["overall"] = data.get("overall")
                        entry["state"] = data.get("state")
                        entry["scenario"] = data.get("scenario")
                    except Exception:
                        pass
                on_disk.append(entry)
        return live + on_disk

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        info = registry.get(run_id)
        if info:
            return info
        report = Path(runs_dir) / run_id / "report.json"
        if not report.exists():
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        import json

        return json.loads(report.read_text(encoding="utf-8"))

    @app.post("/runs/{run_id}/stop")
    def stop_run(run_id: str) -> dict[str, Any]:
        """Request a stop. Returns 202-style acknowledgement; the run ends asynchronously."""
        stopped = registry.stop(run_id)
        if not stopped:
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} is not active or has no sentinel file",
            )
        return {"run_id": run_id, "stop_requested": True}

    @app.get("/runs/{run_id}/events")
    def get_events(run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        from frameforge.kernel.bus import load_events

        path = Path(runs_dir) / run_id / "events.jsonl"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no events for that run")
        events = load_events(path)
        return [e.model_dump(mode="json") for e in events[-limit:]]

    @app.get("/runs/{run_id}/report")
    def get_report(run_id: str, fmt: str = "md") -> Any:
        import json

        paths = RunPaths(root=Path(runs_dir) / run_id)
        if fmt == "json":
            if not paths.report_json.exists():
                raise HTTPException(status_code=404, detail="no report.json")
            return JSONResponse(json.loads(paths.report_json.read_text(encoding="utf-8")))
        if not paths.report_md.exists():
            raise HTTPException(status_code=404, detail="no report.md")
        return {"markdown": paths.report_md.read_text(encoding="utf-8")}

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        """What this machine can actually do right now.

        The dashboard should show this verbatim rather than assuming every feature is
        present - a button that cannot work is worse than a disabled one.
        """
        from frameforge.cli.doctor import run_checks

        return {"checks": [
            {"name": c.name, "status": c.status, "detail": c.detail, "remedy": c.remedy}
            for c in run_checks()
        ]}

    app.state.registry = registry
    app.state.runs_dir = runs_dir
    return app


def serve(runs_dir: Path, host: str = "127.0.0.1", port: int = 8756) -> None:
    """Run the API.

    Refuses a non-loopback bind outright. There is no authentication in this API, so
    exposing it on a network would hand anyone on that network the ability to drive the
    user's machine and read their run artefacts.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        msg = (
            f"refusing to bind the control API to {host!r}. This API has no "
            "authentication and must never be reachable off-host. Use a reverse proxy "
            "with real authentication if remote access is genuinely required."
        )
        raise FrameForgeError(msg)

    import uvicorn

    uvicorn.run(create_app(runs_dir), host=host, port=port, log_level="info")


__all__ = ["RunRegistry", "create_app", "serve"]
