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

Also here: a **report-only reconciler** (`maintenance/services/reconcile.py`) with 8 drift categories,
mutating nothing. It RUNS — its own Dapr cron binding `maintenance-reconcile-cron` on its own
schedule, with read-only FGA + S3 clients (04129b6) — and its unreferenced-FILE pass landed with it
(`maintenance/services/orphans.py`, 3dd3e13, gated by `MAINTENANCE_ORPHAN_SCAN_ENABLED`).

`catalog/api/maintenance_mode.py` is a different thing entirely: read-only maintenance MODE (503 +
Retry-After for a migration window). Renamed in the same commit so the two cannot be confused.

## 2. The reclamation gap — REPORTED (nothing reclaims yet)

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

**Nothing reclaims any of it — but it is now NAMED.** `maintenance/services/orphans.py` lists a
dataset's files and subtracts what any LIVE version references (the union across versions, because
every manifest still in `_versions/` is reachable by time-travel). On the live estate that reports 23
orphans, all `_transactions/*.txn`, and zero data orphans.

Three file classes look like orphans and are NOT — each found by running against a real estate, and
each would have driven a reclaimer into live data: `_refs/tags/*.json` (tags PIN versions),
`data/<stem>/*.blob` (large-binary SIDECARS that `data_files()` does not name — 29 MB of real page
images), and `.lance-reserved`.

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

- **Orphan RECLAMATION** — the report exists (§2); nothing deletes, deliberately, until it has run
  clean on a real estate over time.
- **Reindex from scratch.** `optimize_indices()` folds new fragments into EXISTING indices. Nothing
  rebuilds an index whose parameters changed, and nothing reports one that has drifted.
- **The sweep against real object storage** — the sweep tests use local dirs; only the skipped e2e
  touches S3.
- **Multi-warehouse maintenance** — see §3.

## 5. Order of work

1. ~~Rename the service to `maintenance`.~~ Done 2026-08-04 (06cc757).
2. ~~Wire the reconciler so it can RUN.~~ Done 2026-08-04 (04129b6).
3. ~~The orphan-file detector, report-only.~~ Done 2026-08-04 (3dd3e13).
4. Policy surface: which operations, what cadence, which warehouses. **Read the Lance indexing /
   prune / compaction docs first and report the findings** — that is a standing instruction, not a
   formality: the three false-positive classes in §2 were all things the layout doc alone did not say.
5. Multi-warehouse sweep coverage.
6. Only then consider letting anything delete.

---

Delete this file when the work lands.
