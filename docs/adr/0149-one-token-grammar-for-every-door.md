# 0149. One token grammar for every door (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-300 #311, first answer).

## Context

`/train` validates with `TOKEN_PATTERN` (one segment, no dots: `services/medallion/src/medallion/services/train.py:58`,
`services/medallion/src/medallion/api/train.py:104`). The cascade doors use `SAFE_TOKEN_PATTERN` (`services/medallion/src/medallion/services/trigger_guards.py:38`, `dependencies.py:23`,
`rerun.py:101`), which admits `.`, `.x` and `a.`. A training token is a path segment (`<artifact_base>/<token>/`,
`scripts/ray_train_job.py:9,339`), so `.` would write into the artifact base itself.

## Decision

- One pattern serves every door, and it admits no leading or trailing dot.
- `SAFE_TOKEN_PATTERN` is tightened, and `/train` uses it.

## Consequences

- The cascade doors stop accepting `.`, `.x` and `a.`.
