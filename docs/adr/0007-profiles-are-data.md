# ADR 0007 - Game knowledge lives in profiles, never in the engine

**Status:** Accepted · **Date:** 2026-10-03

## Context

The product promise is universality across game genres, engines, developers and control
schemes. The obvious way to achieve that for one game is to add its keys and coordinates to
the code — which works right up until the second game, and hard-codes knowledge that ought
to be reviewable by the person who owns the game.

## Decision

The engine contains **zero game names, zero screen coordinates, and zero key bindings**.
All of it lives in validated, versioned YAML/JSON under `profiles/`:

- `ControlProfile` — a logical-action vocabulary and its bindings (7 presets ship: fps,
  open_world, racing, rts, sim_builder, sandbox_navigation, ui)
- `GameProfileSpec` — identity, authorised use, landmarks, signals, permitted intents
- `LauncherProfileSpec` — how to reach a title through a visible UI
- `TaskDefinition` / `ScenarioDefinition` — objectives, steps, assertions

Enforced by `TEST-DEV-01`, which greps the engine core for game names and key-binding
literals. It is a failing test, not a convention.

## Consequences

**Accepted benefits**

- Onboarding a title means authoring profiles, not writing code.
- Game knowledge is data: diffable, reviewable, and editable without a rebuild.
- The engine is testable without any game, which is what makes 259 headless tests possible.
- A profile is an authorisation boundary, so authorisation is checked at load time and
  fails closed.

**Accepted costs**

- Authoring profiles is real work, and it is the main practical barrier to using Frame Forge
  on a new title. The honest product statement is: *Frame Forge can drive any Windows UI
  and can play a game once a profile exists for it.*
- Verbose ROI declarations. Normalised regions and click-offset fractions mitigate this.
