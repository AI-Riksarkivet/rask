# open_table_maintenance — reclamation, and the service that owns it

The deletion half of this file is DONE and deleted with it (`DELETE /v1/warehouses/{id}` +
`DELETE /v1/projects/{id}` shipped 2026-08-04, 54fc413; the design that drove them was
`open_hierarchy_lifecycle.md`, now retired). What remains is the half nothing owns: **reclaiming
bytes, and deciding when any of it runs.**

## 1. `services/maintenance` — what it is

Renamed from `services/compaction` (2026-08-04) because it does four things and "compaction" named
one. **Not** `garbage-collector` — compaction and reindex are not GC. **Not** split into
`purge`/`prune`/`compact`/`reindex` services, because the operations are ONE ORDERED PASS over one
dataset (`maintenance.services.optimize.compact_one`):

| Operation | Implementation | Tested |
| --- | --- | --- |
| **compaction** | `ds.optimize.compact_files(defer_index_remap=True)`, falling back to plain compaction when the dataset has no `row_addrs` | ✅ unit + a real-dataset sweep |
| **index updates** | `ds.optimize.optimize_indices()`, counting USER indices only (the `__lance_frag_reuse` system index would otherwise report every compacted dataset as "index maintained" forever) | ✅ incl. the defer-remap interplay and the no-index case |
| **pruning / cleanup** | `ds.cleanup_old_versions(older_than, retain_versions, error_if_tagged_old_versions=False)` — tags are EXEMPT, because the catalog creates long-lived promotion tags and the default `True` would permanently stall GC for that dataset | ✅ retention policy, tag/recent retention |

Compact creates new files and obsoletes old ones, so cleanup must follow it on the same dataset, and
index optimization after that. Four services would mean four scans of every warehouse bucket plus
cross-service ordering to get right. Operations stay modules; each is independently schedulable.

Also here since 54fc413: a **report-only reconciler** (`maintenance/services/reconcile.py`, 7 drift
detectors, mutates nothing) that nothing can yet run — see §4.

`catalog/api/maintenance_mode.py` is a different thing entirely: read-only maintenance MODE (503 +
Retry-After for a migration window). Renamed in the same commit so the two cannot be confused.

## 2. The reclamation gap — now specified, still unbuilt

The Lance spec (lance.org/format/table/layout/ + /transaction/, fetched 2026-08-04) names exactly
what can be orphaned, which is what makes this buildable rather than guesswork:

- `.lance` data files listed in no live manifest;
- deletion vectors (`.arrow` / `.bin`) no fragment references;
- `_indices/<uuid>/` directories absent from every current manifest;
- **`.txn` files from failed or rolled-back commits** — and these accumulate BY DESIGN: on a
  conflict, "transaction files remain in storage describing each commit attempt";
- manifests of deleted versions;
- `_versions/latest_version_hint.json`, which the spec calls "purely an optimization" and "always
  safe to delete".

Our own two producers, on top of those: a partially-failed write, and **a bucket whose warehouse
record was deleted without `?purge_bucket=true`** — which the delete door creates deliberately (a
catalog entry is recoverable, a customer's bucket is not).

**Nothing reclaims any of it.** The phrase "remove orphans" appears nowhere in the service. The
reconciler's orphan-FILE category was specified in Decision 4 and is absent from the shipped module,
undisclosed until an adversarial review caught it.

**Report-only first, always.** A reclaimer that deletes on its first run against a rule nobody has
validated is how a maintenance job eats live data.

## 3. Policy — ours to define, because the format declines to

The Lance table spec is **explicitly silent on garbage collection**: it "doesn't mandate cleanup
timelines for obsolete versions, leaving retention decisions to implementations." There is no
upstream default to inherit and nobody else will decide it.

Today `#50`/`#84` maintenance policies cover retention per table/namespace/project. What has no
policy surface at all: WHICH operations run, HOW OFTEN, and against WHICH warehouses. The sweep is
one Dapr cron binding with one cadence for everything, and `discover_dataset_uris` walks ONE root —
with per-warehouse buckets it must walk EVERY warehouse's root, which is untested across more than
one.

## 4. Still not verified

- **Orphan-file reclamation** — does not exist (§2).
- **The reconciler cannot RUN.** `reconcile()` is called from nowhere, and the service's config
  builds no OpenFGA client — so even once a route exists, four of its seven categories
  (ghost_projects, ghost_warehouses, unreferenced_projects, orphaned_annotation_tasks) report
  UNAVAILABLE forever, including the ghost projects that motivated the whole thing.
- **Reindex from scratch.** `optimize_indices()` folds new fragments into EXISTING indices. Nothing
  rebuilds an index whose parameters changed, and nothing reports one that has drifted.
- **The sweep against real object storage** — the sweep tests use local dirs; only the skipped e2e
  touches S3.
- **Multi-warehouse maintenance** — see §3.

## 5. Order of work

1. ~~Rename the service to `maintenance`.~~ Done 2026-08-04.
2. Wire the reconciler so it can RUN (cron binding + an FGA client on the service).
3. The orphan-file detector, report-only, per the spec list in §2.
4. Policy surface: which operations, what cadence, which warehouses.
5. Multi-warehouse sweep coverage.
6. Only then consider letting anything delete.

---

Delete this file when the work lands.
