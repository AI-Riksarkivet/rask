# 0146. An expired drop releases its grants and its name (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (LH-228's expiry half; reverses diff2 F10 item 5).

## Context

The namespace spec has no undrop. It has two ways to remove a table: DeregisterTable, after which "the table content
remains available in the storage" (`lance_docs/ns_catalog/spec.yaml:4087-4089`) and which the directory catalog keeps
"for potential re-registration" (`lance_docs/ns_catalog/catalog/dir/index.md:66-69`); and DropTable, "Drop table `id`
and delete its data" (`spec.yaml:498`), which may return a transaction id to "track deletion progress" when the data
cannot be deleted at once (`spec.yaml:4049-4051`). RestoreTable restores a version, not a dropped table.

rask's recoverable drop is a deregister plus a trash record: the record is written at
`services/catalog/src/catalog/api/v1/endpoints/tables.py:555-561`, the name's claim is kept (`:564`) and the table is
deregistered (`:565`). Undrop is a re-register (`tables.py:1051-1060`, in `undrop_table` at `:1006`) and ignores the
expiry clock (`tables.py:1022-1033`). A recoverable drop keeps its FGA tuples, because ownership is revoked only when
the drop is not trashed (`tables.py:613-616`), and `require_no_live_trash`
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
- A re-create of the freed name gets a fresh directory under the V2 directory layout, which names a table directory
  `<hash>_<object_id>` so that a table "created, deleted, and recreated with the same name" does not collide
  (`lance_docs/ns_catalog/catalog/dir/index.md:136-146`). A root-namespace table in compatibility mode is named `<table_name>.lance`,
  which is deterministic (`dir/index.md:148`).
- Unconfirmed: whether any rask table sits at a compatibility-mode root, and whether `table_claims.py:33-85`, which
  appears to hold the location separately, lets a re-create resolve to the expired table's location; LH-228 traces both.

## Implementation (LH-228, 2026-10-26)

- Recovery ends at `expires_at` exactly: both undrops refuse a record past it (409).
- The grants go later, by design: the reconcile tick releases a record 15 minutes after its deadline
  (`maintenance.services.trash_release.SETTLE`), on the first tick after that, so up to about 20 minutes at the chart's
  300 s cadence. The settle covers an undrop admitted just before the deadline, which registers the table before it
  clears the record; a revoke landing between the two would leave the restored table ownerless. Measured live on helm
  rev 37: a drop due at 08:20:23 UTC was released at the 08:39:25 UTC tick.
- The name was already free at the drop (LH-525, ADR 0173 §9), so expiry frees nothing further.
- With the purge on, as the dev estate runs it, the purge takes a record at its deadline, before the settle, and its
  revoke ends the grants itself (measured on helm rev 36, 4 minutes past the deadline).
