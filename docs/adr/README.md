# Architecture Decision Records

One file per decision that would be expensive to reverse. Each records the context, the
decision, and — most importantly — the consequences we accepted.

| # | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-loop-ai-plans.md) | The control loop is deterministic; AI only proposes | Accepted |
| [0002](0002-hexagonal-ports.md) | Hexagonal architecture with ports over every Windows API | Accepted |
| [0003](0003-capture-default-mss.md) | mss is the default capture backend, DXGI is opt-in | Accepted |
| [0004](0004-ocr-winrt-persistent-host.md) | WinRT OCR via a persistent PowerShell host | Accepted |
| [0005](0005-input-sendinput.md) | Raw `SendInput` via ctypes, not a convenience library | Accepted |
| [0006](0006-files-not-a-database.md) | Run artefacts are files, not a database | Accepted |
| [0007](0007-profiles-are-data.md) | Game knowledge lives in profiles, never in the engine | Accepted |
| [0008](0008-verdict-integrity.md) | The AI has no write path to any verdict | Accepted |
| [0009](0009-testbed-first.md) | Build our own instrumented target before any real game | Accepted |
