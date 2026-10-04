"""``frameforge doctor`` - the honest capability report.

This command exists because the most expensive failure in a system like this is the user
believing a capability is present when it is not. It reports what is installed, what is
supported, what is degraded, and *why* - including measured performance, since capture and
OCR costs determine what decision rates are actually achievable on this hardware.
"""

from __future__ import annotations

import importlib
import platform
import shutil
import sys
from dataclasses import dataclass

OK = "OK"
WARN = "WARN"
BAD = "BAD"
INFO = "INFO"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str
    remedy: str = ""


def _mod(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def run_checks(verbose: bool = False) -> list[Check]:
    checks: list[Check] = []

    # ------------------------------------------------------------- interpreter
    v = sys.version_info
    checks.append(Check(
        "python", OK if (v.major, v.minor) >= (3, 12) else BAD,
        f"{v.major}.{v.minor}.{v.micro} ({platform.python_implementation()})",
        "Frame Forge requires 3.12+",
    ))
    checks.append(Check(
        "platform", OK if sys.platform == "win32" else BAD,
        f"{sys.platform} / {platform.platform()}",
        "Frame Forge targets Windows",
    ))

    # ------------------------------------------------------------ dependencies
    for module, why, extra in (
        ("mss", "default screen capture", None),
        ("numpy", "array maths", None),
        ("cv2", "template matching, colour probes, PNG encoding", None),
        ("win32gui", "window identity and focus", None),
        ("pydantic", "schema validation (the AI safety layer)", None),
        ("structlog", "structured logging", None),
    ):
        present = _mod(module)
        checks.append(Check(
            f"dep:{module}", OK if present else BAD,
            "installed" if present else "missing", "" if present else f"pip install -e '.[dev]' ({why})"
        ))

    for module, extra, note in (
        ("dxcam", "dxgi", "accelerated capture; mss is the default and always works"),
        ("winsdk", "ocr", "in-process WinRT OCR; the persistent PowerShell host is used otherwise"),
        ("rapidocr_onnxruntime", "ocr_fallback", "fallback OCR for stylized game fonts"),
        ("fastapi", "api", "control API for the future dashboard"),
    ):
        present = _mod(module)
        checks.append(Check(
            f"extra:{module}", OK if present else INFO,
            "installed" if present else f"not installed (optional extra: {extra})",
            "" if present else note,
        ))

    # ------------------------------------------------------------------- ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    checks.append(Check(
        "ffmpeg", OK if ffmpeg else WARN,
        ffmpeg or "not found", "" if ffmpeg else "optional; required only for video evidence",
    ))

    # ------------------------------------------------------- capture (measured)
    if _mod("mss"):
        try:
            from frameforge.adapters.capture.mss_capture import MssCaptureSource
            from frameforge.ports.capture import Surface, SurfaceKind
            from frameforge.ports.geometry import Size

            surface = Surface(kind=SurfaceKind.MONITOR, size=Size(1920, 1080))
            cap = MssCaptureSource()
            cap.open(surface)
            times = [cap.grab().capture_ms for _ in range(6) if cap.grab() is not None]
            cap.close()
            if times:
                mean = sum(times) / len(times)
                rate = 1000.0 / mean if mean else 0
                status = OK if mean < 60 else (WARN if mean < 150 else BAD)
                checks.append(Check(
                    "capture:mss", status,
                    f"~{mean:.0f}ms/frame at 1920x1080 (~{rate:.1f} fps ceiling)",
                    "slower than expected" if status != OK else "",
                ))
        except Exception as exc:
            checks.append(Check("capture:mss", BAD, f"{type(exc).__name__}: {exc}"))

    # ---------------------------------------------------------- monitors/layout
    if _mod("win32gui"):
        try:
            from frameforge.adapters.window.pywin32_window import PyWin32WindowAdapter

            win = PyWin32WindowAdapter()
            topo = win.monitors()
            desc = "; ".join(
                f"{m.device_name} {m.rect.width}x{m.rect.height}{' (primary)' if m.is_primary else ''}"
                for m in topo.monitors
            )
            checks.append(Check("displays", OK, desc or "none reported"))
            scales = {m.dpi for m in window.monitors_detailed()}
            if len(scales) > 1:
                checks.append(Check(
                    "display-scaling", WARN,
                    f"MIXED SCALING across monitors: dpi={sorted(scales)}",
                    "a point can be correct for one display and wrong for another",
                ))
            else:
                checks.append(Check(
                    "display-scaling", OK,
                    f"uniform {sorted(scales)[0] if scales else 'unknown'} dpi"))
            checks.append(Check(
                "virtual desktop", INFO,
                f"{topo.virtual_rect.width}x{topo.virtual_rect.height} at "
                f"({topo.virtual_rect.x},{topo.virtual_rect.y})",
            ))
            session = win.session_state()
            checks.append(Check(
                "session", OK if session.interactive else BAD,
                f"{session} (console sid {win.console_session_id()})",
                "" if session.interactive else "input is blocked on an inactive session",
            ))
        except Exception as exc:
            checks.append(Check("windows", BAD, f"{type(exc).__name__}: {exc}"))

    # --------------------------------------------------------------------- OCR
    if verbose:
        try:
            from frameforge.adapters.ocr.winrt_ocr import WinRtOcr

            ocr = WinRtOcr()
            caps = ocr.capabilities()
            if caps.available:
                checks.append(Check(
                    "ocr", OK, f"{caps.primary} languages={list(caps.languages)} (measured separately)",
                ))
                ocr.close()
            else:
                checks.append(Check(
                    "ocr", WARN, f"unavailable: {caps.notes}",
                    "text assertions will evaluate to UNKNOWN; install the 'ocr' extra",
                ))
        except Exception as exc:
            checks.append(Check("ocr", WARN, f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------------- AI
    ai_key = bool(__import__("os").environ.get("FRAMEFORGE_AI_API_KEY"))
    ai_on = _mod("openai") or True
    checks.append(Check(
        "ai planner", INFO if ai_key else INFO,
        "configured" if ai_key else "not configured (this is fine: ai: off is fully supported)",
        "set FRAMEFORGE_AI_API_KEY to enable a remote planner" if not ai_key else "",
    ))

    # -------------------------------------------------------------- authorised
    checks.append(Check(
        "authorised use", INFO,
        "Frame Forge refuses profiles without a valid authorized_use attestation "
        "(docs/AI_GUARDRAILS.md G-AUTH-01)",
    ))

    return checks


def format_checks(checks: list[Check]) -> str:
    lines = ["Frame Forge capability report", "=" * 74, ""]
    width = max(len(c.name) for c in checks) if checks else 10
    for c in checks:
        mark = {OK: "[ ok ]", WARN: "[warn]", BAD: "[BAD ]", INFO: "[info]"}[c.status]
        lines.append(f"{mark} {c.name.ljust(width)}  {c.detail}")
        if c.remedy and c.status in (BAD, WARN):
            lines.append(f"       {' ' * width}  -> {c.remedy}")
    lines.append("")
    lines.append("Notes:")
    lines.append("  * Optional extras degrade capability; they never prevent the core from running.")
    lines.append("  * ai: off is a complete, supported mode - see docs/ROADMAP.md stability criterion 10.")
    return "\n".join(lines)


__all__ = ["Check", "format_checks", "run_checks"]
