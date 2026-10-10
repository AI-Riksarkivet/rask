# 0146. An expired drop releases its grants and its name (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-228's expiry half; reverses diff2 F10 item 5). Reference: Lakekeeper's expiration worker.

## Context

Undrop ignores the expiry clock (`services/catalog/src/catalog/api/v1/endpoints/tables.py:1022-1033`). A
recoverable drop keeps its FGA tuples (`tables.py:567,594,615`), and `require_no_live_trash` (`services/catalog/src/catalog/api/fga_deps.py:1069-1094`)
holds the name until the purge runs. The purge ships off (`chart/values.yaml:1871`) and runs only when the drift report
is clean, so in the default chart a dropped table's grants and name lock last indefinitely.

## Decision

- At `expires_at`, a maintenance reconcile step revokes the object's tuples (emitting `grant_revoked`), marks the
  trash record `expired` and frees the name. The bytes wait for the purge.
- Undrop of an expired record is refused.

## Consequences

- No stale grants and no name held by a table nobody can recover.
- A drop past its grace period is not recoverable, even while its bytes still exist.
