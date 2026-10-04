"""Shared fixtures.

Every test in this suite runs headless. That is a deliberate architectural constraint,
not a convenience: it means the engine's logic is testable on a machine with no display,
in CI, and on a machine where the operator is asleep. Tests that need the real desktop are
marked ``hardware`` and excluded by default.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from frameforge.kernel.bus import EventLog
from frameforge.kernel.clock import FakeClock
from frameforge.ports import fakes
from frameforge.ports.capture import Surface, SurfaceKind
from frameforge.ports.geometry import Size

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def surface() -> Surface:
    return Surface(kind=SurfaceKind.WINDOW, size=Size(1280, 720), hwnd=1000, label="test")


@pytest.fixture
def capture(surface: Surface) -> fakes.FakeCapture:
    cap = fakes.FakeCapture(surface)
    cap.open(surface)
    return cap


@pytest.fixture
def window() -> fakes.FakeWindow:
    return fakes.FakeWindow(title="Test Target", class_name="TestClass", process_name="test.exe")


@pytest.fixture
def ocr() -> fakes.FakeOcr:
    return fakes.FakeOcr(default=["MENU", "PLAY"])


@pytest.fixture
def vision() -> fakes.FakeVision:
    return fakes.FakeVision(template_scores={"any": 0.95})


@pytest.fixture
def input_port() -> fakes.FakeInput:
    return fakes.FakeInput()


@pytest.fixture
def event_log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl", "test-run", clock=FakeClock())


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


def pytest_addoption(parser: pytest.Parser) -> None:
    """Hardware tests are opt-in.

    The default suite must pass on a machine with no display, because that is the
    guarantee that makes the engine testable in CI and unattended. Hardware tests touch
    the real desktop and the real input queue, so running them requires saying so.
    """
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help="run tests that require a real Windows desktop session and real input",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "hardware: requires a real desktop session")
    config.addinivalue_line("markers", "slow: long-running")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip hardware tests unless explicitly requested."""
    if config.getoption("--run-hardware"):
        return
    skip = pytest.mark.skip(reason="needs a real desktop; pass --run-hardware to enable")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)
