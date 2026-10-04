"""A release must never disarm the manager.

Found by the first live POC run. ``InputSafetyManager.release_all()`` ends with
``self.enabled = False``. The executor releases on every batch exit and the controller
releases after every dispatched primitive, so the first action disarmed the manager and
every later press was refused with "input disarmed".

The whole mock suite missed this because every mock port accepts primitives regardless of
its armed state - a mock cannot observe a disarm.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "frameforge"


class TestReleaseDoesNotDisarm:
    def test_release_all_leaves_the_manager_armed(self):
        """The regression: a cleanup sweep must not revoke press authority."""
        from frameforge.actions.safety import InputSafetyManager
        from frameforge.ports.input import PrimitiveType

        mgr = InputSafetyManager()
        mgr.arm()
        assert mgr.enabled is True
        mgr.release_all()
        assert mgr.enabled is True, (
            "release_all() disarmed the manager; every later press is then refused")

    def test_two_releases_still_allow_a_press(self):
        """The exact live sequence: click, release, then type."""
        from frameforge.actions.safety import InputSafetyManager
        from frameforge.ports.input import PrimitiveType

        mgr = InputSafetyManager()
        mgr.arm()
        mgr.release_all()          # as the click's balance check does
        mgr.release_all()          # and as the controller does after dispatch
        assert mgr.enabled is True

    def test_repeated_release_all_never_disarms(self):
        from frameforge.actions.safety import InputSafetyManager

        mgr = InputSafetyManager()
        mgr.arm()
        for _ in range(10):
            mgr.release_all(thorough=False)
        assert mgr.enabled is True

    def test_disarm_is_still_available_explicitly(self):
        """The fix must not remove the ability to stop input on purpose."""
        from frameforge.actions.safety import InputSafetyManager

        mgr = InputSafetyManager()
        mgr.arm()
        mgr.disarm()
        assert mgr.enabled is False


class TestTheVisibleFailureShape:
    def test_disarmed_press_is_recorded_as_such(self):
        """It was recorded - but not counted as a violation, so no summary showed it."""
        source = (SRC / "actions" / "safety.py").read_text(encoding="utf-8")
        seg = source[source.index("if not self.enabled and not releasing:"):]
        seg = seg[:seg.index("return False")]
        assert "Violation.DISARMED" in seg
        assert "_record" in seg
        # A refused press must be visible in the violation summary, not only per-event.
        assert "self.violations" in seg or "self.blocked" in seg, (
            "a refused press is invisible in any aggregate the report reads")


class TestTheMockNowModelsTheDisarm:
    def test_fake_port_refuses_a_press_when_the_manager_is_disarmed(self):
        """Contract parity is what would have caught this on day one.

        The fake used to consult only its own ``_enabled`` flag, so it accepted presses the
        real adapter refused. A mock that is more permissive than the thing it mocks cannot
        fail when the real thing does.
        """
        from frameforge.ports import fakes

        port = fakes.FakeInput()
        port.set_enabled(True)
        assert port.safety.enabled is True
        # Simulate the live defect: the manager is disarmed behind the fake's back.
        port.safety.disarm()
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        port.send(Primitive(kind=PrimitiveType.KEY, key=Key.A, down=True))
        assert port.primitives == [], (
            "the fake accepted a press the real port would refuse; contract parity broken")

    def test_fake_still_accepts_releases_when_disarmed(self):
        """Releases bypass the arm gate by design; the fake must agree."""
        from frameforge.ports import fakes
        from frameforge.ports.input import Key, Primitive, PrimitiveType

        port = fakes.FakeInput()
        port.set_enabled(True)
        port.safety.disarm()
        port.send(Primitive(kind=PrimitiveType.KEY, key=Key.A, down=False))
        assert len(port.primitives) == 1, "a release must never be blocked by the arm gate"
