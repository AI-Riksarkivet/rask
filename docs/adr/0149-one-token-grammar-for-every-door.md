# 0149. One token grammar for every door (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-300 #311, first answer).

## Context

At the decision `/train` validated with `TOKEN_PATTERN` (one segment, no dots), and the medallion service's cascade
doors used `SAFE_TOKEN_PATTERN`, which admits `.`, `.x` and `a.`. A training token was a path segment
(`<artifact_base>/<token>/`), so `.` would have written into the artifact base itself. Those doors and the training job
left with `services/medallion` (273a17a43, LH-464), so no token pattern remains; the decision binds the next door that
takes a token as a path segment.

## Decision

- One pattern serves every door, and it admits no leading or trailing dot.
- `SAFE_TOKEN_PATTERN` is tightened, and `/train` uses it.

## Consequences

- The cascade doors stop accepting `.`, `.x` and `a.`.
