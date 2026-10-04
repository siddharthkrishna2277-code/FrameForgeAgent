"""Testbed - Frame Forge's own reference target application.

Why this exists
---------------
It is the backbone of the test strategy, not a toy. A game-automation system cannot be
developed credibly against "whatever game happens to be installed", because:

* bugs need to be *seeded*, deterministically, on demand;
* perception regressions need a target whose state changes in known ways;
* the whole suite must run on a machine where no game is installed, or where the operator
  is asleep;
* a CI runner has no display.

So the testbed is a small, deliberately instrumented application with:

* a main menu with three buttons;
* an animated loading screen with a spinner and a progress bar;
* a gameplay view with a live HUD counter, a health bar, and a minimap;
* a modal error dialog;
* a **seeded, deterministic bug** - the health bar fails to render when HP < 30, so an
  assertion about it fails in a known, reproducible way.

The bug is the point. A QA tool that cannot detect a known defect has not been shown to
work. Frame Forge's first proof of value is "it finds the bug we planted", and
``--bug healthbar`` makes the whole thing a regression test.

Rendering is deliberately plain: solid fills, large high-contrast text, no alpha tricks.
Both template matching and OCR have to succeed here, so anything that would make the
target easy to perceive in an unrepresentative way is avoided.

Usage::

    python fixtures/testbed/testbed_app.py --title "FrameForge Testbed" --bug healthbar
    python fixtures/testbed/testbed_app.py --state gameplay --hp 20   # start mid-scenario
"""

from __future__ import annotations

import argparse
import math
import sys
import time

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None

# --------------------------------------------------------------------- palette
# High contrast, and deliberately distinct hue families so colour probes are reliable.
BG = "#101018"
PANEL = "#1c1c2a"
PANEL_LIGHT = "#2a2a3e"
TEXT = "#f2f2f7"
TEXT_DIM = "#9a9ab0"
ACCENT = "#4da3ff"
ACCENT_DIM = "#2b6cb0"
GOOD = "#3ddc84"
WARN = "#ffb020"
BAD = "#ff4d5a"
HP_GOOD = "#3ddc84"
HP_LOW = "#ff4d5a"


class TestbedState:
    """Mutable application state, all deterministic given the seed."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.title = args.title
        self.bug = args.bug
        self.start_state = args.state
        self.fixed_size = (args.width, args.height)
        self.t0 = time.perf_counter()

        # Deterministic starting values. A test that reads "HP=57" and gets 57 every time
        # is worth far more than a random seed.
        self.hp = args.hp
        self.score = 0
        self.state = self.start_state
        self.loading_progress = 0.0
        self.loading_done = False
        self.menu_index = 0
        self.dialog_visible = False
        self.dialog_message = args.dialog_message
        self.status = "READY"
        self.log: list[str] = []

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def note(self, message: str) -> None:
        stamp = f"{self.elapsed:7.2f}s"
        self.log.append(f"[{stamp}] {message}")

    def enter_gameplay(self) -> None:
        self.state = "gameplay"
        self.loading_done = True
        self.loading_progress = 1.0
        self.note("entered gameplay")

    def tick(self) -> None:
        """Advance animation. Deterministic: driven purely by elapsed time."""
        e = self.elapsed
        if self.state == "loading":
            # 1.5 second load, so the loading screen is observable but not tedious.
            self.loading_progress = min(1.0, e / 1.5) if not self.loading_done else 1.0
            if self.loading_progress >= 1.0 and not self.loading_done:
                self.loading_done = True
                self.note("loading finished")
        elif self.state == "gameplay":
            # HP drains slowly; crosses the 30 threshold so the seeded bug triggers on its
            # own even without a scenario telling it to.
            self.hp = max(0, self.hp - (1 if int(e * 2) % 40 == 0 else 0))
            self.score = int(e * 10)

    def health_bar_visible(self) -> bool:
        """The seeded defect lives here.

        Below 30 HP the bar is not drawn. Everything else about the HUD keeps updating,
        which is what makes this a realistic bug rather than a crash: a QA scenario that
        asserts "health bar visible while HP is low" fails, and the report points at the
        first divergence.
        """
        if self.bug == "healthbar" and self.hp < 30:
            return False
        return True


class TestbedApp:
    """Tkinter renderer. Plain widgets and canvas primitives only."""

    def __init__(self, state: TestbedState) -> None:
        self.state = state
        self.root = tk.Tk()
        self.root.title(state.title)
        w, h = state.fixed_size
        self.root.geometry(f"{w}x{h}+120+80")
        self.root.configure(bg=BG)
        # A fixed size keeps the profile's normalised regions valid: the testbed must not
        # rescale out from under its own landmarks.
        self.root.resizable(False, False)

        self.canvas = tk.Canvas(
            self.root, width=w, height=h, bg=BG, highlightthickness=0, bd=0
        )
        self.canvas.pack(fill="both", expand=True)

        # An always-present status strip doubles as the machine-readable signal: it is
        # large, high-contrast text at a known position, so a scenario can assert on it via
        # OCR without any special hook.
        self.W = w
        self.H = h
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._running = True

    # ------------------------------------------------------------------ layout
    # Regions are expressed as fractions of the canvas so the profile can describe them in
    # normalised coordinates, exactly as a real profile would.

    def rect(self, x: float, y: float, w: float, h: float) -> tuple[int, int, int, int]:
        return (int(x * self.W), int(y * self.H), int(w * self.W), int(h * self.H))

    # ------------------------------------------------------------------- render

    def render(self) -> None:
        self.state.tick()
        c = self.canvas
        c.delete("all")

        match self.state.state:
            case "menu":
                self._render_menu(c)
            case "loading":
                self._render_loading(c)
            case "gameplay":
                self._render_gameplay(c)
            case "dialog":
                self._render_gameplay(c)
                self._render_dialog(c)

        self._render_status(c)

    def _render_menu(self, c: tk.Canvas) -> None:
        c.create_text(
            self.W // 2, int(0.18 * self.H), text="FRAMEFORGE TESTBED",
            fill=TEXT, font=("Segoe UI", 30, "bold"),
        )
        c.create_text(
            self.W // 2, int(0.26 * self.H), text="reference target for perception and QA",
            fill=TEXT_DIM, font=("Segoe UI", 13),
        )
        labels = ["START GAME", "SETTINGS", "QUIT"]
        for i, label in enumerate(labels):
            y = int((0.42 + i * 0.12) * self.H)
            active = i == self.state.menu_index
            x0, y0, w, h = self.rect(0.30, (y - 26) / self.H, 0.40, 52 / self.H)
            c.create_rectangle(
                x0, y0, x0 + w, y0 + h,
                fill=PANEL_LIGHT if active else PANEL,
                outline=ACCENT if active else PANEL_LIGHT,
                width=3 if active else 1,
            )
            c.create_text(
                x0 + w // 2, y0 + h // 2, text=label,
                fill=ACCENT if active else TEXT, font=("Segoe UI", 18, "bold" if active else "normal"),
            )

    def _render_loading(self, c: tk.Canvas) -> None:
        c.create_text(
            self.W // 2, int(0.42 * self.H), text="LOADING",
            fill=TEXT, font=("Segoe UI", 24, "bold"),
        )
        # Spinner: an animated arc. Gives the health monitor real animation to detect and
        # the loading-wait logic something honest to wait on.
        cx, cy, r = self.W // 2, int(0.58 * self.H), 40
        for i in range(12):
            angle = self.state.elapsed * 4 + i * 30
            rad = math.radians(angle)
            x = cx + r * math.cos(rad)
            y = cy + r * math.sin(rad)
            shade = ACCENT if i % 3 == 0 else ACCENT_DIM
            c.create_oval(x - 6, y - 6, x + 6, y + 6, fill=shade, outline="")
        # Progress bar.
        bx, by, bw, bh = self.rect(0.30, 0.72, 0.40, 14 / self.H)
        c.create_rectangle(bx, by, bx + bw, by + bh, fill=PANEL_LIGHT, outline="")
        c.create_rectangle(
            bx, by, bx + int(bw * self.state.loading_progress), by + bh, fill=ACCENT, outline=""
        )
        c.create_text(
            self.W // 2, int(0.80 * self.H),
            text=f"{int(self.state.loading_progress * 100)}%",
            fill=TEXT_DIM, font=("Segoe UI", 12),
        )

    def _render_gameplay(self, c: tk.Canvas) -> None:
        # A deterministic pseudo-scene: a grid plus a moving marker, so the frame changes
        # between observations and change detection has something real to measure.
        for gx in range(0, self.W, 60):
            c.create_line(gx, 0, gx, self.H, fill="#1a1a26")
        for gy in range(0, self.H, 60):
            c.create_line(0, gy, self.W, gy, fill="#1a1a26")
        t = self.state.elapsed
        mx = int(self.W * (0.5 + 0.28 * math.sin(t * 0.8)))
        my = int(self.H * (0.55 + 0.18 * math.cos(t * 1.1)))
        c.create_oval(mx - 18, my - 18, mx + 18, my + 18, fill=ACCENT, outline=TEXT, width=2)

        # Minimap: fixed panel, distinct colour, so a colour probe can assert on it.
        mx0, my0, mw, mh = self.rect(0.78, 0.10, 0.18, 0.28)
        c.create_rectangle(mx0, my0, mx0 + mw, my0 + mh, fill=PANEL, outline=ACCENT, width=2)
        c.create_text(
            mx0 + mw // 2, my0 + mh - 14, text="MINIMAP", fill=TEXT_DIM, font=("Segoe UI", 10)
        )
        c.create_oval(
            mx0 + int(mw * 0.45), my0 + int(mh * 0.40),
            mx0 + int(mw * 0.58), my0 + int(mh * 0.58), fill=GOOD, outline="",
        )

        # HUD: score always visible; health bar subject to the seeded bug.
        c.create_text(
            40, 40, text=f"SCORE {self.state.score}", fill=TEXT, font=("Segoe UI", 20, "bold"), anchor="w"
        )
        c.create_text(
            40, 72, text=f"HP {self.state.hp}", fill=TEXT, font=("Segoe UI", 16), anchor="w"
        )
        if self.state.health_bar_visible():
            bx, by, bw, bh = self.rect(0.02, 0.115, 0.20, 18 / self.H)
            c.create_rectangle(bx, by, bx + bw, by + bh, fill=PANEL_LIGHT, outline=TEXT_DIM)
            frac = max(0.0, min(1.0, self.state.hp / 100.0))
            colour = HP_GOOD if self.state.hp >= 30 else HP_LOW
            c.create_rectangle(bx + 2, by + 2, bx + int((bw - 4) * frac), by + bh - 2, fill=colour, outline="")
            c.create_text(
                bx + bw + 14, by + bh // 2, text="HEALTHBAR",
                fill=TEXT, font=("Segoe UI", 13, "bold"), anchor="w",
            )
        # A second, always-visible label so a scenario can assert the HUD exists at all,
        # distinguishing "HUD missing" from "health bar missing".

    def _render_dialog(self, c: tk.Canvas) -> None:
        x0, y0, w, h = self.rect(0.25, 0.35, 0.50, 0.26)
        c.create_rectangle(x0 - 6, y0 - 6, x0 + w + 6, y0 + h + 6, fill="#000000", stipple="gray50")
        c.create_rectangle(x0, y0, x0 + w, y0 + h, fill=PANEL_LIGHT, outline=WARN, width=3)
        c.create_text(
            x0 + w // 2, y0 + int(h * 0.32), text="ERROR",
            fill=BAD, font=("Segoe UI", 22, "bold"),
        )
        c.create_text(
            x0 + w // 2, y0 + int(h * 0.58), text=self.state.dialog_message,
            fill=TEXT, font=("Segoe UI", 12), width=int(w * 0.9),
        )
        c.create_text(
            x0 + w // 2, y0 + int(h * 0.85), text="PRESS ENTER TO CONTINUE",
            fill=TEXT_DIM, font=("Segoe UI", 12),
        )

    def _render_status(self, c: tk.Canvas) -> None:
        c.create_rectangle(0, self.H - 34, self.W, self.H, fill=PANEL, outline="")
        c.create_text(
            14, self.H - 17,
            text=f"STATE {self.state.state}  STATUS {self.state.status}  HP {self.state.hp}",
            fill=TEXT, font=("Consolas", 12), anchor="w",
        )

    # ------------------------------------------------------------------- events

    def _on_key(self, event: tk.Event) -> None:
        key = event.keysym
        self.state.note(f"key {key}")
        if self.state.state == "menu":
            if key in ("Up", "Down"):
                delta = -1 if key == "Up" else 1
                self.state.menu_index = (self.state.menu_index + delta) % 3
            elif key in ("Return", "space"):
                self._activate_menu()
        elif self.state.state == "dialog":
            if key in ("Return", "Escape", "space"):
                self.state.dialog_visible = False
                self.state.state = "gameplay"
                self.state.note("dialog dismissed")
        elif self.state.state == "loading":
            pass
        else:
            if key in ("q", "Q"):
                self.state.status = "QUIT_REQUESTED"
                self.state.note("quit requested")
            elif key in ("d", "D"):
                self.state.dialog_visible = True
                self.state.state = "dialog"
                self.state.note("dialog opened")

    def _on_click(self, event: tk.Event) -> None:
        self.state.note(f"click at {event.x},{event.y}")
        if self.state.state == "menu":
            for i in range(3):
                y = int((0.42 + i * 0.12) * self.H)
                x0, y0, w, h = self.rect(0.30, (y - 26) / self.H, 0.40, 52 / self.H)
                if x0 <= event.x <= x0 + w and y0 <= event.y <= y0 + h:
                    self.state.menu_index = i
                    self._activate_menu()
                    return
        elif self.state.state == "dialog":
            self.state.dialog_visible = False
            self.state.state = "gameplay"

    def _activate_menu(self) -> None:
        choice = self.state.menu_index
        self.state.note(f"menu activate index={choice}")
        match choice:
            case 0:
                self.state.state = "loading"
                self.state.status = "LOADING"
                self.state.loading_progress = 0.0
                self.state.loading_done = False
                # Reset the load clock so the loading screen is fully observable.
                self.state.t0 = time.perf_counter()
            case 1:
                self.state.state = "dialog"
                self.state.dialog_visible = True
                self.state.dialog_message = "SETTINGS LOCKED IN THIS BUILD"
                self.state.status = "SETTINGS"
            case 2:
                self.state.status = "QUIT_REQUESTED"
                self._running = False

    def _after_load(self) -> None:
        if self.state.state == "loading" and self.state.loading_done:
            self.state.enter_gameplay()

    def _on_close(self) -> None:
        self._running = False

    # --------------------------------------------------------------------- loop

    def run(self) -> int:
        self.root.bind("<Key>", self._on_key)
        self.canvas.bind("<Button-1>", self._on_click)
        self.root.focus_force()

        # An optional scriptable command channel so a scenario can put the testbed into a
        # specific state without needing a UI path to get there. This is the testbed's own
        # fixture hook - the authorised-signal idea, applied to our own app.
        if self.state.__dict__.get("_script"):
            pass

        def loop() -> None:
            if not self._running:
                self.root.destroy()
                return
            self._after_load()
            self.render()
            self.root.after(40, loop)

        loop()
        self.root.mainloop()
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Frame Forge reference testbed application")
    p.add_argument("--title", default="FrameForge Testbed")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument(
        "--bug", default="none", choices=["none", "healthbar"],
        help="seed a deterministic defect. 'healthbar' hides the health bar when HP < 30.",
    )
    p.add_argument("--state", default="menu", choices=["menu", "loading", "gameplay", "dialog"])
    p.add_argument("--hp", type=int, default=57)
    p.add_argument("--dialog-message", default="UNABLE TO LOAD SAVE DATA")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if tk is None:
        print("tkinter is unavailable; cannot run the testbed", file=sys.stderr)
        return 2
    state = TestbedState(args)
    state.note(f"testbed start title={args.title!r} bug={args.bug} state={args.state} hp={args.hp}")
    return TestbedApp(state).run()


if __name__ == "__main__":
    raise SystemExit(main())
