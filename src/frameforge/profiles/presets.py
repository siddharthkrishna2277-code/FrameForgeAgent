"""Control-profile presets for the six interaction models.

This is where "universal" is actually implemented. The engine knows these *intent names*
and nothing else; each preset binds them to physical keys for a family of games. Onboarding
a new title means choosing a preset and overriding what differs - no code change, and no
game name anywhere in the codebase (guardrail G-DEV-01).

Presets are deliberately generous about aliases: a binding may list several keys for the
same intent so a profile can cover both WASD and arrow-key schemes without a second
preset.
"""

from __future__ import annotations

from frameforge.actions.compiler import Binding, ControlProfile
from frameforge.ports.input import Key, MouseButton


def _k(*keys: Key) -> Binding:
    return Binding(kind="key", keys=keys)


def fps_preset() -> ControlProfile:
    """First/third-person shooter: WASD + mouselook + weapon hotkeys."""
    p = ControlProfile(name="fps", look_sensitivity=0.35, look_steps=14, look_duration_ms=140)
    p.bind("forward", _k(Key.W))
    p.bind("backward", _k(Key.S))
    p.bind("strafe_left", _k(Key.A))
    p.bind("strafe_right", _k(Key.D))
    p.bind("jump", _k(Key.SPACE))
    p.bind("crouch", _k(Key.LCTRL, Key.C))
    p.bind("prone", _k(Key.Z, Key.LCTRL))
    p.bind("sprint", _k(Key.LSHIFT))
    p.bind("walk", _k(Key.LALT))
    p.bind("dodge", _k(Key.LSHIFT, Key.A))
    p.bind("interact", _k(Key.E))
    p.bind("use", _k(Key.E))
    p.bind("reload", _k(Key.R))
    p.bind("fire", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("aim", Binding(kind="mouse_button", button=MouseButton.RIGHT))
    p.bind("melee", _k(Key.V, Key.F))
    p.bind("weapon_next", _k(Key.X))
    p.bind("weapon_prev", _k(Key.Z))
    p.bind("grenade", _k(Key.G))
    p.bind("map", _k(Key.M, Key.TAB))
    p.bind("inventory", _k(Key.TAB))
    p.bind("pause", _k(Key.ESCAPE))
    p.bind("confirm", _k(Key.ENTER, Key.E))
    p.bind("back", _k(Key.ESCAPE))
    p.bind("debug_overlay", _k(Key.F1))
    return p


def open_world_preset() -> ControlProfile:
    """Open-world/action: same core plus camera, mounts, quest and map navigation."""
    p = fps_preset()
    p.name = "open_world"
    p.look_sensitivity = 0.30
    p.bind("camera_left", _k(Key.Q))
    p.bind("camera_right", _k(Key.E))
    p.bind("pickup", _k(Key.F))
    p.bind("journal", _k(Key.J))
    p.bind("quests", _k(Key.L))
    p.bind("eat", _k(Key.B))
    p.bind("sleep", _k(Key.Z))
    p.bind("hotbar_1", _k(Key.N1))
    p.bind("hotbar_2", _k(Key.N2))
    p.bind("hotbar_3", _k(Key.N3))
    p.bind("hotbar_4", _k(Key.N4))
    return p


def racing_preset() -> ControlProfile:
    """Racing: steering, throttle, brake, handbrake, camera."""
    p = ControlProfile(name="racing", look_sensitivity=0.5)
    p.bind("accelerate", _k(Key.W, Key.UP))
    p.bind("brake", _k(Key.S, Key.DOWN))
    p.bind("steer_left", _k(Key.A, Key.LEFT))
    p.bind("steer_right", _k(Key.D, Key.RIGHT))
    p.bind("handbrake", _k(Key.SPACE))
    p.bind("camera_left", _k(Key.Q))
    p.bind("camera_right", _k(Key.E))
    p.bind("look_back", _k(Key.S))
    p.bind("restart", _k(Key.R))
    p.bind("pause", _k(Key.ESCAPE))
    p.bind("confirm", _k(Key.ENTER))
    p.bind("back", _k(Key.ESCAPE))
    p.bind("camera", Binding(kind="axis", axis="right_y", value=1.0))
    return p


def rts_preset() -> ControlProfile:
    """RTS/strategy: mouse-driven, hotkeys, camera panning."""
    p = ControlProfile(name="rts")
    p.bind("select_all", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("deselect", Binding(kind="mouse_button", button=MouseButton.RIGHT))
    p.bind("select_box", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("command", Binding(kind="mouse_button", button=MouseButton.RIGHT))
    p.bind("unit_place", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("building_menu", _k(Key.B))
    p.bind("pan_left", _k(Key.A))
    p.bind("pan_right", _k(Key.D))
    p.bind("pan_up", _k(Key.W))
    p.bind("pan_down", _k(Key.S))
    p.bind("camera_left", _k(Key.Q))
    p.bind("camera_right", _k(Key.E))
    p.bind("select_next", _k(Key.TAB))
    p.bind("confirm", _k(Key.ENTER))
    p.bind("back", _k(Key.ESCAPE))
    p.bind("pause", _k(Key.ESCAPE))
    p.bind("debug_overlay", _k(Key.F2))
    return p


def sim_builder_preset() -> ControlProfile:
    """City builder / simulation: placement, pan/zoom, panel management."""
    p = ControlProfile(name="sim_builder")
    p.bind("place_block", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("place_block_alt", Binding(kind="mouse_button", button=MouseButton.RIGHT))
    p.bind("rotate", _k(Key.R))
    p.bind("menu", _k(Key.ESCAPE, Key.TAB))
    p.bind("building_menu", _k(Key.B))
    p.bind("zoom_in", _k(Key.EQUALS, Key.PAGEUP))
    p.bind("zoom_out", _k(Key.MINUS, Key.PAGEDOWN))
    p.bind("pan_up", _k(Key.W))
    p.bind("pan_down", _k(Key.S))
    p.bind("pan_left", _k(Key.A))
    p.bind("pan_right", _k(Key.D))
    p.bind("pan_up_edge", _k(Key.UP))
    p.bind("pan_down_edge", _k(Key.DOWN))
    p.bind("confirm", _k(Key.ENTER))
    p.bind("back", _k(Key.ESCAPE))
    p.bind("pause", _k(Key.ESCAPE))
    p.bind("save", _k(Key.LCTRL, Key.S))
    return p


def sandbox_preset() -> ControlProfile:
    """Sandbox / survival / navigation: movement, hotbar, interaction, building."""
    p = ControlProfile(name="sandbox_navigation", look_sensitivity=0.35)
    p.bind("forward", _k(Key.W))
    p.bind("backward", _k(Key.S))
    p.bind("strafe_left", _k(Key.A))
    p.bind("strafe_right", _k(Key.D))
    p.bind("jump", _k(Key.SPACE))
    p.bind("sprint", _k(Key.LSHIFT))
    p.bind("crouch", _k(Key.LCTRL))
    p.bind("interact", _k(Key.E))
    p.bind("pickup", _k(Key.F))
    p.bind("build", _k(Key.B))
    p.bind("craft", _k(Key.C))
    p.bind("inventory", _k(Key.TAB, Key.E))
    p.bind("map", _k(Key.M))
    p.bind("hotbar_1", _k(Key.N1))
    p.bind("hotbar_2", _k(Key.N2))
    p.bind("hotbar_3", _k(Key.N3))
    p.bind("hotbar_4", _k(Key.N4))
    p.bind("hotbar_5", _k(Key.N5))
    p.bind("rotate", _k(Key.R))
    p.bind("confirm", _k(Key.ENTER))
    p.bind("back", _k(Key.ESCAPE))
    p.bind("pause", _k(Key.ESCAPE))
    p.bind("menu", _k(Key.TAB))
    p.bind("debug_overlay", _k(Key.F1))
    return p


def ui_preset() -> ControlProfile:
    """Ordinary Windows UI / launcher: keyboard and mouse only, no look."""
    p = ControlProfile(name="ui")
    p.text_method = "unicode"
    p.bind("confirm", _k(Key.ENTER))
    p.bind("cancel", _k(Key.ESCAPE))
    p.bind("back", _k(Key.ESCAPE, Key.BACKSPACE))
    p.bind("tab_next", _k(Key.TAB))
    p.bind("menu", _k(Key.ESCAPE, Key.ALT))
    p.bind("search", _k(Key.LCTRL, Key.F))
    p.bind("scroll_up", _k(Key.UP, Key.PAGEUP))
    p.bind("scroll_down", _k(Key.DOWN, Key.PAGEDOWN))
    p.bind("click", Binding(kind="mouse_button", button=MouseButton.LEFT))
    p.bind("right_click", Binding(kind="mouse_button", button=MouseButton.RIGHT))
    p.bind("drag", Binding(kind="mouse_button", button=MouseButton.LEFT))
    return p


PRESETS: dict[str, callable] = {
    "fps": fps_preset,
    "open_world": open_world_preset,
    "racing": racing_preset,
    "rts": rts_preset,
    "sim_builder": sim_builder_preset,
    "sandbox_navigation": sandbox_preset,
    "ui": ui_preset,
}


def get_preset(name: str) -> ControlProfile:
    factory = PRESETS.get(name)
    if factory is None:
        msg = f"unknown control preset {name!r}; available: {sorted(PRESETS)}"
        raise KeyError(msg)
    return factory()


__all__ = ["PRESETS", "get_preset"]
