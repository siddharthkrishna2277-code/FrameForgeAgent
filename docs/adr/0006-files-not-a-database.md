# ADR 0006 - Run artefacts are files, not a database

**Status:** Accepted · **Date:** 2026-10-03

## Context

Every run produces observations, decisions, actions, verdicts, frames, video, and a report.
That data needs to be stored, queryable, and handed to a developer.

## Decision

A run directory is the evidence bundle, and it is the database:

```
runs/ff-<timestamp>-<rand>/
  manifest.json     run id, seed, versions, profile hashes, launch mode, argv
  events.jsonl      every consequential event, append-only
  plan.jsonl        the exact logical action sequence, replayable
  timeline.jsonl    state transitions with timestamps and confidence
  frames/           key frames, plus every FAIL and UNKNOWN
  video/run.mp4     optional, via external ffmpeg
  ocr/              text snapshots at decision points
  report.json       machine-readable verdict set
  report.md         human/dev-readable
  junit.xml         optional CI export
```

## Consequences

**Accepted benefits**

- Zero migration cost, zero schema drift, zero database dependency.
- A run directory is a zip file. It survives the project, the tool, and the future.
- Append-only JSONL survives a crash: the most valuable moments to lose an audit trail
  are exactly when a run dies. A truncated final line is tolerated on read.

**Accepted costs**

- No cross-run SQL queries. For the primary use case — one run per report, read by one
  developer — that is the right trade.
- A run index over many runs is directory scanning. Adequate at the scale a single laptop
  produces, with `prune_old_runs` for retention.
- If cross-run trend analysis becomes a requirement (the "build 41 fails, build 42 passes"
  view), a small index layer should be added on top rather than replacing the files.
