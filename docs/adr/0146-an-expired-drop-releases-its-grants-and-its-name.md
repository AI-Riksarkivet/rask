# 0146. An expired drop releases its grants and its name (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-228's expiry half; reverses diff2 F10 item 5).

## Context

The namespace spec has no undrop. It has two ways to remove a table: DeregisterTable, after which "the table content
remains available in the storage" (`lance_docs/ns_catalog/spec.yaml:4079-4081`) and which the directory catalog keeps
"for potential re-registration" (`lance_docs/ns_catalog/catalog/dir/index.md:66-70`); and DropTable, "Drop table `id`
and delete its data" (`spec.yaml:498`), which may return a transaction id to "track deletion progress" when the data
cannot be deleted at once (`spec.yaml:4041-4043`). RestoreTable restores a version, not a dropped table.

rask's recoverable drop is a deregister plus a trash record (`services/catalog/src/catalog/api/v1/endpoints/tables.py:524`),
and undrop is a re-register (`services/catalog/src/catalog/services/dataplane.py:783`). Undrop ignores the expiry clock
(`tables.py:1022-1033`). A recoverable drop keeps its FGA tuples (`tables.py:567,594,615`), and `require_no_live_trash`
(`services/catalog/src/catalog/api/fga_deps.py:1069-1094`) holds the name until the purge runs. The purge ships off
(`chart/values.yaml:1871`) and runs only when the drift report is clean, so in the default chart a dropped table's
grants and name lock last indefinitely.

## Decision

- A recoverable drop is a DeregisterTable whose data stays for re-registration, until `expires_at`. Expiry withdraws
  the right to re-register: from then on the table is in DropTable's state, its data deleted asynchronously.
- At `expires_at`, a maintenance reconcile step revokes the object's tuples (emitting `grant_revoked`), marks the
  trash record `expired` and frees the name. The bytes wait for the purge.
- Undrop of an expired record is refused.

## Consequences

- No stale grants and no name held by a table nobody can recover.
- A drop past its grace period is not recoverable, even while its bytes still exist.
- Unconfirmed: whether a re-create of the freed name could resolve to the expired table's still-present location;
  `table_claims.py:33-85` appears to hold the location separately and LH-228 traces it.
