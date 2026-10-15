# 0162. The lakehouse builds nothing: compaction and indexing commit through `version/create`, and maintenance decides (2026-10-15)

**Status:** accepted. Source: owner, 2026-10-15, through the multi-question tool while taking LH-467 (#587). Amends
ADR 0156 C4 and C6.

## Context

ADR 0156 C6 kept `CreateTableIndex` as a bounded asynchronous build inside the catalog, and C4 had a bring-your-own
rewrite job commit through "the catalog's compaction and index commit doors". ADR 0157 then made `version/create` the
door every stock pylance commit goes through, judged by the catalog, so a compaction or an index no longer needs a door
of its own. The owner ruled on 2026-10-15 that compute-heavy table work is bring-your-own: a build in a catalog pod puts
column-sized memory on the pod that serves every request, and a size cap only moves where it fails.

## Decision

1. **The lakehouse builds nothing.** Compaction, index creation and index optimize run on the caller's compute and
   commit through `version/create`. `CreateTableIndex` and `CreateTableScalarIndex` answer 406 `Unsupported` naming
   that path. The catalog's compaction plan and commit doors, `maintenance/compact`, `maintenance/reindex`, the
   merge-key index build on `merge_insert`, and maintenance's worker with its `MAINTENANCE_WORK` and
   `MAINTENANCE_INDEX` streams are deleted.
2. **`version/create` judges each such commit**, on top of ADR 0157's judge:
   - Lance's own compaction protocol (`Compaction.plan`, `task.execute`, `Compaction.commit`) makes two commits,
     `ReserveFragments` then `Rewrite` (measured on pylance 13.0.0). Both need `can_maintain` beyond the writer rung;
     a `CreateIndex` needs the writer rung.
   - `ReserveFragments` may only advance `max_fragment_id`.
   - A `CreateIndex` changes no data. Each new segment lives under the table's own `_indices/<uuid>/`, through no
     base, with every file it declares present at its declared size, and the index must open. A JSON index is not
     opened: Lance has no statistics for it and panics.
   - A `Rewrite` keeps every row: by stable row id where the table has them, by count otherwise. It passes the
     compaction evidence gate (`maintenance.require_compactable`).
   - The judge exists because Lance itself commits each of these broken shapes (measured on pylance 13.0.0): a
     rewrite that loses rows, a rewrite that duplicates rows under one stable row id, and an index naming a base or an
     empty directory.
3. **A compaction is judged against the estate's clone list, and fails closed.** Compacting a dataset another dataset
   resolves its files through deletes what the clone reads, and the evidence lives only on the referring side. The
   maintenance sweep writes the list each tick (`_maintenance_due/state/clones.json` on the control root).
   `version/create` refuses a compaction 503 while the list is missing or older than `cloneListMaxAgeSeconds` (default
   3600, one chart value for both readers), and 406 when the table is a clone's source.
4. **Maintenance decides.** The sweep writes one due record per table that needs compacting or an index optimize:
   the counts, the policy record that won, and how many clones read through it. The records live under
   `_maintenance_due/`, and `GET /management/v1/maintenance/due` lists them to a caller holding `can_maintain`. The
   list is advisory, not a gate.
5. **Version expiry is commit-driven.** The write-event lane expires the table that was just written, once it has
   `minVersionsToExpire` (20) reclaimable versions. The sweep becomes the backstop, every 30 minutes (below the clone
   list's maximum age). Both lanes ask one eligibility rule, `service_kit.lakehouse.version_retention`, which the
   catalog's GC preview shares.
6. **What stays.** Trash purge, the orphan scan, reconcile and protection stay in maintenance. The catalog keeps
   `maintenance/preview` and `maintenance/run`, which list and delete only. Erasure keeps its in-catalog compaction and
   index rebuild as C6's named exception. Moving it to bring-your-own compute is parked as LH-493 (#637).

## Considered options

- **A bounded build of `CreateTableIndex` in the catalog's pods** (C6, and a size-capped index queue proposed the same
  morning): rejected by the owner. It is fatal-intensive work on the pod that serves every request.
- **Catalog doors of their own for compaction plans and commits**: superseded by ADR 0157's `version/create`, which
  judges what a stock client commits. A second door would be a second judge.
- **Expiry on the sweep's tick alone**: replaced by commit-driven expiry with a threshold. Lakekeeper schedules
  snapshot expiry right after a commit once 20 are due. Polaris stores maintenance policies for engines and runs none
  of them (`docs/audits/2026-10-15/polaris-maintenance/maintenance.md`). Both are inspiration only.

## Consequences

- A pipeline that wants a compacted table runs Lance's own compaction as an identity holding `writer` and
  `maintainer` on it. An index is built, rebuilt or re-tuned by its owner's own compute.
- A table that nobody writes is expired by the 30-minute backstop, and a stopped sweep shows as compactions refused
  503 once the clone list ages out.
- A vector index built on a table too small to train keeps its shrunken partition count through an erasure's rebuild
  (lance #9134). Re-tuning it is the owner's build.
