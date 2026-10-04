# Multi-monitor / input-isolation safety report

**No code changed in producing this report.** Live global input testing is stopped.

---

## 1. Root cause — with a correction to the initial hypothesis

The report's hypothesis is *"Hermes is calling something equivalent to 'right-click here'
rather than 'move to the verified element'."* I audited for exactly that. **It is not
present in Frame Forge's code, and I think the observed behaviour is a testing artifact
rather than a product defect — but there is one real gap, and it is a design gap, not an
implementation bug.**

### What is already correct

| Requirement | Status |
|---|---|
| Explicit move before every button press | **Yes.** `_compile_click` emits `MOUSE_MOVE_ABS` whenever `at` is present, before the button down. |
| Virtual-desktop absolute mapping | **Yes.** `MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`, normalised across the whole virtual desktop, not the primary monitor. |
| Virtual-screen negative/offset origins | **Yes.** `Surface.offset_x/offset_y` come from `ClientToScreen`; `_abs_norm` uses `SM_CXVIRTUALSCREEN`. |
| Capture honours the target's offset | **Yes.** `MssCaptureSource` passes `offset_x/offset_y` into the BitBlt rect. |
| Right-click refused by default | **Yes.** `DenyReason.RIGHT_CLICK`, off unless `allow_right_click`; independently refused by `InputSafetyManager`. |
| Deny-list incl. Hermes/Electron UI | **Yes.** `Chrome_WidgetWin_1` and the agent process are deny-listed. |
| Foreground must be the target | **Yes.** `InputPolicy` requires foreground == target hwnd. |
| Shipped scenarios clicking "here" | **No.** Zero shipped scenarios issue a click without an explicit point. |

### The real gap

**A `Click` with no `at` compiles to a button press at whatever position the cursor
happens to occupy.** That is precisely the failure mode described — and it is a genuine
hole in the action model, even though no shipped scenario exercises it:

```python
# actions/compiler.py::_compile_click
if a.at is not None:
    prims.append(Primitive(kind=MOUSE_MOVE_ABS, ...))   # only when a point exists
for _ in range(a.count):
    prims.append(Primitive(kind=MOUSE_BUTTON, down=True, ...))   # else: at the cursor
```

Nothing in the schema, the compiler, or the policy layer rejects `Click(at=None)`. A
scenario author — or an AI planner — could write one and silently click wherever the
operator's hand happens to be. **That is the fix that matters, and it is small: an
"act at the current cursor position" action must not be expressible.**

### On the observed behaviour

Reproducing what you saw: the *hardware tests* are the only code that clicks a fixed
screen point, and they are **guarded** — `require_test_target()` skips unless the
foreground window is a registered, non-deny-listed target. When it does run, it sends to a
hardcoded `(300, 300)` **on the primary monitor**.

So the mechanism that explains your observation is: a hardware test ran with a foreground
window that passed the guard, clicked `(300, 300)` on the primary monitor, and — depending
on which monitor Notepad occupied at that moment — the click landed in Notepad or in the
agent's own window. Not a coordinate-conversion bug; **an unguarded-by-design fixed screen
point in a test that is supposed to be guarded.**

### What is genuinely missing (your requirements 5–8, 10)

| Requirement | Present? |
|---|---|
| `TestTargetSession` (hwnd, pid, executable identity, monitor, rects, allowed regions, DPI, topology fingerprint, expiry) | **No** — no session object exists |
| `TargetGuard` validating window-under-point before dispatch | **No** — foreground is checked, the window *under the point* is not |
| Protected-window registry by PID/HWND (not title matching) | **Partial** — deny-list is class/process name, not a live registry |
| Per-monitor DPI awareness | **No** — no DPI handling at all |
| Topology-change detection mid-run → cancel pending input | **No** — signature is tracked for change detection but never cancels anything |
| UI Automation for element targeting | **No** — coordinate/OCR only |
| Configurable test display | **No** |

---

## 2. Input API / backend used

`user32.SendInput` via ctypes, in `actions/safety.py::InputSafetyManager`. Absolute mouse
movement uses `MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`, normalising over
`SM_CXVIRTUALSCREEN × SM_CYVIRTUALSCREEN`. No `pyautogui`, no `pynput`, no `keyboard`,
no `mouse`, no `mouse_event`, no low-level **mouse** hook. A low-level **keyboard** hook
exists solely for the estop hotkey.

## 3. Multi-monitor coordinate model

Four spaces, currently related by two functions:

```
surface-local px ──(Surface.offset_x/y, from ClientToScreen)──▶ virtual-desktop physical px
virtual-desktop px ──(_abs_norm, over SM_CX/CYVIRTUALSCREEN)──▶ SendInput 0..65535
```

Capture works in surface-local px and is offset at capture time. Actions are authored in
surface-local px and offset at compile time. Both use the same `Surface.offset_*`, so a
profile written against one monitor works on another.

**Missing:** named types per space (they are bare `int`s — which is how mixing happens),
runtime bounds assertions, DPI normalisation, and any window-under-point validation.

## 4. Target / vision implementation status

- **Vision:** exists and is wired. `MssCaptureSource` → `FrameAssembler` → landmarks/OCR
  → `Observation` → the planner sees a `PerceptionSummary` of anchor scores and OCR lines.
  A `click_anchor` action resolves its point from the landmark's matched rect **at decision
  time**, so it does not depend on a stale authored coordinate.
- **Right-click planning from stale cursor coordinates:** no. `click_anchor` uses the
  matched rect; `click` uses an authored surface-local point.
- **Not claimed:** that a right-click has been proven to land on a specific Notepad element.
  Right-click is refused by default and **no shipped scenario uses one**.

---

## 5. Immediate safety measures taken

1. **Live testing stopped.** No further global input injection will be performed.
2. **Run `frameforge recover-input`** — done; state verified clean (no stuck modifiers,
   no layout change, agent window correctly non-injectable).
3. **Hardware tests are guarded** and currently skip: 6 of them report *"refusing to inject
   into a protected window"* because `explorer.exe` is foreground and deny-listed.
4. **The remaining unguarded case is design, not behaviour** — see the gap above.

---

## 6. Recommended fixes, in the order I would make them

Nothing below has been implemented. This is the plan for approval.

**P0 — close the current-cursor hole (small, high value).**
Make `Click(at=None)`, `MouseButtonDown` and `MouseButtonUp` *inexpressible* without a
validated point. A cursor-relative action becomes a distinct type requiring an explicit
`relative_to_anchor` reference. Test: no primitive batch may contain a button down whose
preceding move is absent.

**P1 — `TestTargetSession` + `TargetGuard`.**
A session object binding run_id → hwnd, pid, verified image path, monitor handle, client
and screen rects, allowed regions, DPI, topology fingerprint, expiry. Validated immediately
before **every** dispatch; on failure: zero input, cancel pending, release all, report
`target_validation_failed` with a precise reason. This is the single biggest structural
addition and directly addresses requirements 5 and 6.

**P2 — window-under-point verification.**
`WindowFromPoint` at the computed point, plus `GetAncestor(GA_ROOT)`, must resolve to the
approved target before a button event. Closes "the click landed somewhere else" entirely.

**P3 — protected-window registry.**
Register the agent's own top-level windows by PID/HWND at startup and maintain them by
ownership rather than title. Reject any action whose point falls inside one. Independent
of the class-name deny-list, which can drift.

**P4 — named coordinate types + bounds assertions.**
`SurfacePx`, `ScreenPx`, `VirtualPx`, `NormalizedInputPx`. Distinct types make accidental
mixing a type error rather than a wrong click.

**P5 — DPI awareness.**
Per-monitor-DPI-aware process, target DPI retrieval, and one physical-pixel space across
capture, window rects and cursor. No hard-coded compensation.

**P6 — topology fingerprint cancels pending input.**
Already computed for change detection; wire it to cancel and require revalidation.

**P7 — UI Automation for element targeting.**
Preferred over coordinate clicks for Notepad/File Explorer per requirement 10. Larger
effort; the P0–P3 work makes coordinate clicking safe in the meantime, so this is an
improvement in reliability rather than a safety prerequisite.

---

## 7. Honest assessment

The report's most valuable insight is not "you have a coordinate bug" — it is
**"never let an action act at the current cursor position."** Frame Forge nearly satisfies
that already: clicks carry explicit points, coordinates are virtual-desktop aware, and
right-click is refused outright. The hole is narrow and real (`Click(at=None)`), and it is
a schema-level fix, not a rewrite.

Where the report is right and Frame Forge is not yet adequate: there is **no target
session**, **no window-under-point check**, **no protected-window registry**, and **no DPI
handling**. On a two-monitor desktop with the operator's pointer wherever they left it,
those are exactly the gaps that would let a click land in the wrong window.

**Recommendation: implement P0–P3 before any further live testing.** P0 alone closes the
only path that can act at the cursor. Until P1–P3 land, live multi-monitor testing should
not run with the agent's own UI visible on another monitor — and even then only with
`--refocus` and a registered target, never by hand-placing the pointer.

---

## 8. IMPLEMENTED: P0-P3 (approval given after this audit)

All verification below is read-only or mock-backend. **No live input was injected.**

### P0 — the current-cursor path is now inexpressible

Proven by import, not by assertion:

```
refused: Click           -> ValidationError      with a point: ok
refused: MouseButtonDown -> ValidationError      with a point: ok
refused: MouseButtonUp   -> ValidationError      with a point: ok
```

Three layers, deliberately independent:
1. **Schema** - `at: Point` is required (was `Point | None`).
2. **Compiler** - `_require_point()` refuses with an explanatory error.
3. **Runtime** - a button-down with no preceding verified move is refused by
   `_refuse_target(...)` before anything reaches the port.

`CursorClick` was added for the genuinely cursor-relative case: it requires
`require_session`, so even that must name the target authorising it.

### P0.5 — no fixed screen coordinate anywhere

`grep` for `MOUSE_MOVE_ABS, x=<int>, y=<int>` across `tests/` returns **none**.
Two literals in the policy tests were replaced with a derived constant, because a reader
cannot distinguish "harmless test value" from "unsafe fixed point" and the pattern itself
is the hazard. `TestNoFixedCoordinateInjection` asserts the invariant statically, with a
static check on the tests themselves.

### P1 — `TargetSession` (`actions/target.py`, exported as `TestTargetSession`)

Binds run → hwnd, pid, `process_name`, **verified image path**, client + screen rects,
monitor index/device, **DPI**, approved regions (normalised), topology fingerprint,
`require_foreground`, `max_age_ms`, and `protected_pids`. Re-validated before every
action: expiry, `IsWindow`, `IsIconic`, pid change, client-origin drift, DPI change,
topology fingerprint change.

### P2 — `WindowFromPoint`, checked twice

`verify_point` runs **before the cursor moves** and **again immediately before
button-down**, resolving `GetAncestor(GA_ROOT)` so a click on a child control still
identifies the target. Foreground-only validation is explicitly not sufficient; both are
required, and either failure produces `target_validation_failed` with a precise reason.

A real gap this surfaced: the original code moved the cursor *first* and checked
afterwards, which would already have relocated the pointer onto whatever was covering the
target. The pre-move check is now in place.

### P3 — `ProtectedRegistry`

Tracks the agent's own windows by **pid and hwnd**, not by title or class text, so
renaming, resizing, moving, or showing a context menu does not defeat it. `protect_class`
complements it for classes the agent may spawn later.

### Also fixed, and found only because these tests ran

* **Normalisation bias.** `VirtualDesktop.to_send_input` divided by `width - 1`, which
  skewed every interior point: the midpoint of a 3840-wide desktop normalised to 32776
  instead of 32767 - roughly a **9-pixel bias** at the centre of a two-monitor setup.
  Corrected to divide by the full extent.
* **`GetDpiForWindow` lives in user32**, not `shcore` (which only exports
  `GetDpiForMonitor`). The natural guess raised `AttributeError` on this machine.
* **A release needs no point of its own.** P0 initially made `MouseButtonUp` require one,
  which broke every click: the release follows the press and must not move the cursor.
  It is anchored to the press it answers.

---

## 9. Acceptance results

`tests/unit/test_target_acceptance.py` — **11/11 pass** against a mock backend, asserting
on *emissions* rather than intent, because the point of the gate is that refusal happens
before the OS is touched.

| # | Scenario | Result |
|---|---|---|
| A | Pointer over Hermes, valid click on registered Notepad region | **moves to the validated point, clicks there only**; a move always precedes the button |
| B | Agent's own window foreground | **refused, zero events**; refusal carries a reason plus window evidence |
| C | Pointer at 5 different positions (incl. laptop, edge, other monitor) | **outcome identical in every case**; every emitted move is the validated target point |
| D | Display topology changed | **refused before injection**; message states revalidation is required |
| E | Target moved after planning | **stale action refused** |
| G | Planned point inside a protected window | **hard denial, zero events**; audit records what was actually under the point |

Each refusal carries: the requested point, target hwnd/pid, `WindowFromPoint` result,
foreground window, the verdict, and the cleanup state.

## 10. Honest statement of what is still not done

* **P4 (typed coordinate spaces) and P5 (DPI awareness)** are implemented in the module
  (`SurfacePx`/`ScreenPx`/`SendInputPx`/`VirtualDesktop`, per-monitor DPI context and
  per-window DPI) but **not yet wired through the runner**, so a live run does not yet
  construct a `TargetSession` from the resolved window.
* **No live run has been performed since P0-P3.** The gate is proven against a mock
  backend and against the real desktop's *geometry* (monitors, DPI, topology fingerprint),
  but no click has been dispatched through the full live path with a registered target.
  That is the next step, and it needs your go-ahead.
* **The Notepad scenario's OCR-recall limitation is unchanged** and still open.
* **UI Automation (P6) and a VM/sandbox runner (P7)** are not started. P7 remains the
  strongest long-term containment.
