# 0204. Maintenance's commits are the catalog's, and its vend puts nothing of a table (2026-11-12)

**Status:** accepted. Source: [[LH-575]] (#797), audit item M7 of the 2026-11-09 code audit, folded into that row as a
pre-approved zero-trust gap (CLAUDE.md § Four facts). Amends [ADR 0197](0197-maintenance-writes-only-under-a-vend-and-no-workload-holds-a-store-key.md)
decision 3, and the "a maintain-tier credential" in [ADR 0187](0187-a-re-announcement-is-signed-only-for-the-author-the-catalog-recorded.md)'s
context.

## Context

Read on main `9f202053b`: maintenance's pass made two commits of its own, straight to the store under the table's
maintain-tier vend: the disarm of Lance's commit-path cleanup (`expiry.py:193-218`, a `delete_config_keys`) and the
listing-floor raise (`floor.py:223`, an `update_config` stamping `rask.maintenance.floor_raised_at`). For both, the vend
granted put, delete and abort-upload on the whole table prefix but `_refs/`, so it could put a final manifest: commit any
operation around every door, `version/create` included. The commit reconciler exempted every config-only `UpdateConfig`
from authorship (`commit_reconcile.py:405-410`), so a config commit made past every door was neither announced nor
counted, and the two maintenance commits were attributed to nobody.

Lakekeeper's answer is the shape of an Iceberg catalog: every commit goes through the catalog's commit door, and each is
audited with the actor of the request that made it (`crates/lakekeeper/src/service/events/backends/audit/assemble.rs`,
`actor(request_metadata, …)`, read at main `08b67d7`). Lance puts the commit's compare-and-swap in the object store
(`lance_docs/lance/format/table/transaction.md` § Commit Protocol), so the same property holds here only if no
credential the catalog vends can put a final manifest. The write tier already cannot (ADR 0157, LH-469).

Measured on pylance 13.0.0 behind a counting proxy: `cleanup_old_versions` issues GET, HEAD and bulk-delete POST and no
PUT; `update_config` PUTs a transaction file and a final manifest; `delete_config_keys` records each key in
`config_updates` mapped to `None`, `replace` false, the other three maps empty.

## Decision

1. **The maintain tier gets and deletes, and puts nothing of a table.** `catalog.core.vending._MAINTAIN_ACTIONS` is
   get and delete on the table's prefix, its branch directories and its own data directories; the explicit Deny keeps
   every delete off `_refs/`. The vend still puts the table's history fence on the control root, the one record a reclaim
   writes under it ([[LH-219]]). The warehouse scope probe, which lands and removes its own objects under a prefix that
   holds no table, gets a tier of its own, `probe`, which no door vends.
2. **Maintenance's two commits are the catalog's, at maintainer doors.** `POST /management/v1/table/{id}/maintenance/disarm`
   removes a ref's `lance.auto_cleanup.*` keys, and `POST /management/v1/table/{id}/maintenance/floor` commits the floor
   stamp on main, stamped on the catalog's own clock (the one that times the raise's manifest) and committed through
   `commit_stamped`, so the catalog records the caller as the version's author (ADR 0187). Both sit at `can_maintain`,
   which the maintenance identity holds and every owner keeps; a floor raise's `dry_run` is judged as the raise is and
   commits nothing, so maintenance's preview meets the refusal the raise would.
3. **The reconciler exempts a pure disarm alone.** A version whose `UpdateConfig` removes `lance.auto_cleanup.*` keys and
   nothing else is the commit every door makes before its own and deletes nothing, so it needs no author. A floor raise is
   confirmed by its recorded author and announced on no feed. Any other config commit is a door's, its author recorded,
   or was made past every door and is counted unauthored.

## Consequences

- Only the catalog's own credential and the store's root can put a final manifest, and every commit but a pure disarm
  carries an author the catalog recorded or is counted.
- A maintenance pod holding a vend can still delete what a cleanup deletes on the table it was vended; the history fence
  and the refusal ladder bound that, as before.
- The disarm keeps its conditions: maintenance asks for it only on a table vended the maintain tier, so a held table
  keeps its keys until a door's commit disarms it.
- `lineage.core.reconcile.MAINTENANCE_OPERATIONS`, which `floor.py` cited as why lineage ignores a floor raise, no longer
  exists; the catalog's reconciler is what reads the raise, by its recorded author.
