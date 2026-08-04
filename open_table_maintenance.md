# open_table_maintenance — reclamation, and the service that owns it

The deletion half of this file is DONE and deleted with it (`DELETE /v1/warehouses/{id}` +
`DELETE /v1/projects/{id}` shipped 2026-08-04, 54fc413; the design that drove them was
`open_hierarchy_lifecycle.md`, now retired). What remains is the half nothing owns: **reclaiming
bytes, and deciding when any of it runs.**

Everything below §2 was revised 2026-08-04 after reading the Lance table-format specs end to end
(`format/table/*`, the index specs, the performance guide, and the data-evolution guide). Several
claims this file previously made were wrong, and one shipped detector is wrong — see §3.

## 1. `services/maintenance` — what it is

Renamed from `services/compaction` (06cc757) because it does four things and "compaction" named one.
**Not** `garbage-collector` — compaction and reindex are not GC. **Not** split into
`purge`/`prune`/`compact`/`reindex` services, because the operations are ONE ORDERED PASS over one
dataset (`maintenance.services.optimize.compact_one`):

| Operation | Implementation | Tested |
| --- | --- | --- |
| **compaction** | `ds.optimize.compact_files(defer_index_remap=True)`, falling back to plain compaction when the dataset has no `row_addrs` | ✅ unit + a real-dataset sweep |
| **index updates** | `ds.optimize.optimize_indices()`, counting USER indices only (the `__lance_frag_reuse` system index would otherwise report every compacted dataset as "index maintained" forever) | ✅ incl. the defer-remap interplay and the no-index case |
| **pruning / cleanup** | `ds.cleanup_old_versions(older_than, retain_versions, error_if_tagged_old_versions=False)` — tags are EXEMPT, because the catalog creates long-lived promotion tags and the default `True` would permanently stall GC for that dataset | ✅ retention policy, tag/recent retention |

Compact obsoletes files, so cleanup must follow it on the same dataset, and index optimization after
that. Four services would mean four scans of every warehouse bucket plus cross-service ordering.

Also here: a **report-only reconciler** (`maintenance/services/reconcile.py`) with 8 drift categories,
mutating nothing. It RUNS — its own Dapr cron binding `maintenance-reconcile-cron` (04129b6) — and
its unreferenced-FILE pass landed with it (`maintenance/services/orphans.py`, 3dd3e13, gated by
`MAINTENANCE_ORPHAN_SCAN_ENABLED`).

`catalog/api/maintenance_mode.py` is a different thing: read-only maintenance MODE (503 +
Retry-After for a migration window). Renamed in the same commit so the two cannot be confused.

## 2. The reclamation gap — REPORTED (nothing reclaims yet)

The spec names what can be unreferenced: `.lance` data files in no live manifest; deletion vectors no
fragment references; `_indices/<uuid>/` absent from every manifest; **`.txn` files from failed or
rolled-back commits, which accumulate BY DESIGN** ("transaction files remain in storage describing
each commit attempt"); manifests of deleted versions; and `_versions/latest_version_hint.json`
("purely an optimization", "always safe to delete").

Plus our own two producers: a partially-failed write, and a bucket whose warehouse record was deleted
without `?purge_bucket=true` (the delete door creates those deliberately).

On the live estate the pass reports **23 orphans, all `_transactions/*.txn`, zero data orphans.**

**Four file classes look like orphans and are NOT.** Three were caught by running against a real
estate; the fourth by reading the spec. Each would drive a reclaimer into live data:

1. `_refs/tags/*.json` — tags PIN versions (`cleanup_old_versions` exempts tagged versions).
2. `data/<stem>/*.blob` — large-binary SIDECARS that `data_files()` does not name. The first live run
   called 29 MB of real page images reclaimable.
3. `.lance-reserved` — a structural marker.
4. `_refs/branches/*.json` — branch metadata, excluded today only incidentally by the `_refs/` rule.

**Report-only first, always.**

## 3. KNOWN WRONG — the orphan pass on branches, clones and multi-base

`referenced_paths()` opens only the MAIN branch of a dataset. The spec makes two cases fatal:

- **Branches.** A branch is a shallow clone whose `_versions/`, `_transactions/`, `_deletions/` and
  `_indices/` live under `tree/{branch_name}/`. None of that is reachable from the main branch, so
  **every file in every branch is unreferenced by construction** and would be reported. Branch names
  may contain `/`, so `tree/bugfix/issue-123/` is a nested path, not one segment.
- **Shallow clones / multi-base.** A manifest carries `base_paths[]`; any DataFile, DeletionFile or
  index with `base_id` set resolves under ANOTHER dataset root. So (a) a clone appears to be missing
  files it never held, and (b) the SOURCE's files are referenced by clones we never open — scanning
  the source alone calls them garbage.

Until fixed, the pass MUST skip (unavailable-with-reason) any dataset whose manifest declares
`base_paths` or whose root contains `tree/`. Feature flag 16 (`FLAG_BASE_PATHS`) is the signal.

## 4. Policy — ours to define, but NOT all of it

The table spec is explicitly silent on GC ("leaving retention decisions to implementations"). But
Lance ships **its own auto-cleanup**, which this file previously did not account for:

```
lance.auto_cleanup.interval    # every N commits
lance.auto_cleanup.older_than  # e.g. "3600s"
# AutoCleanupConfig at write_dataset, ds.optimize.enable/disable_auto_cleanup,
# write_dataset(..., skip_auto_cleanup=True)
```

It runs ON THE COMMIT PATH (needs delete permission, adds write latency); the docs offer a periodic
background job as the alternative, which is what our sweep already is. So the policy surface must
decide per operation which layer owns it — version cleanup may need no cron of ours at all, while
compaction / index optimize / reconcile / orphan-report have no upstream equivalent.

`cleanup_old_versions(older_than=timedelta(0))` is SAFE (never deletes the current version).
**`delete_unverified=True` is documented as "extremely dangerous"** — it can delete an in-flight
operation's files. Our sweep must never pass it.

Still unowned: WHICH operations run, HOW OFTEN, against WHICH warehouses. `discover_dataset_uris`
walks ONE root; with per-warehouse buckets it must walk every one, untested across more than one.

## 5. What the specs added that we had not considered

- **Compaction vs indices.** Rewriting files invalidates row addresses, so by default compaction
  remaps EVERY index — which makes compaction and index-building conflict, and "typically the
  compaction would fail, resulting in table layout degrading over time". `defer_index_remap=True` +
  the **Fragment Reuse Index** is the fix, and our sweep already passes it. But the FRI GROWS per
  compaction and must be trimmed once indices are rebuilt past a reuse version, or index LOAD cost
  climbs forever. Nothing trims it.
- **Reindex-from-scratch is not expressible.** `optimize_indices()` folds new fragments into EXISTING
  indices. A changed index type or parameter, or an `alter_columns` cast (which **silently drops the
  column's index**), needs a full rebuild. FTS partition count is fixed at build time.
- **Batch sizing.** Scans can use `2*io_buffer_size + batch_size*num_compute_threads`. The default
  8192-row batch against ~1.8 MB bronze page rows is ~15 GB per compute thread. The sweep almost
  certainly needs a smaller `batch_size`.
- **Caches are per dataset object, not global.** Metadata cache 1 GiB, index cache 6 GiB
  (`index_cache_size_bytes`; the entries-based knob is deprecated). "Create a single table and share
  it" — worth checking the catalog is not discarding both on every request.
  `dataset.session().size_bytes()` reports real usage.
- **Lance emits the events we were about to invent**: `lance::file_audit` (file created/deleted, by
  type), `lance::execution` (iops, bytes_read, indices_loaded, parts_loaded), `lance::io_events`
  (index loads that MISSED the cache), `lance::object_store::throttle`. Wire to OTLP.
- **Fragment sizing is a real tradeoff.** Manifest work scales with fragment COUNT; conflict
  detection is PER-FRAGMENT. "If you run many concurrent updates, deletes, or merge_insert, err
  toward MORE fragments" — which is exactly the annotator's write pattern.
- **Feature flags are the refusal mechanism.** Flag 64 (data overlay files): a reader that does not
  understand them "must refuse a dataset that uses them… a correctness bug rather than a degraded
  experience." Unknown flags ≥32 must be rejected.
- **NOT YET MINED**: `mem_wal.md` (LSM shards, `_mem_wal/` tree, SSTable compaction order, and a
  documented warning that GC'ing WAL files WEAKENS writer fencing) and `data_overlay_file.md`
  (overlay→overlay merges must be contiguous in `committed_version`; an overlay→base fold on an
  indexed field must rebuild that index in the same commit). Both are maintenance-relevant.

## 6. Still not verified

- **Orphan RECLAMATION** — the report exists; nothing deletes, deliberately.
- **Reindex from scratch** — §5.
- **The sweep against real object storage** — sweep tests use local dirs; only the skipped e2e
  touches S3. (The reconcile + orphan passes HAVE been run against the live rustfs.)
- **Multi-warehouse maintenance** — §4.

## 7. Order of work

1. ~~Rename the service to `maintenance`.~~ Done (06cc757).
2. ~~Wire the reconciler so it can RUN.~~ Done (04129b6).
3. ~~The orphan-file detector, report-only.~~ Done (3dd3e13) — **but see §3, it is wrong for
   branches and clones and must be gated before anyone trusts it.**
4. Policy surface, now including the auto-cleanup decision in §4.
5. Multi-warehouse sweep coverage.
6. Only then consider letting anything delete.

Tracked as tasks #51, #55, #57–#64.

---

Delete this file when the work lands.
