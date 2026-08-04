# open_table_maintenance — reclamation, and the service that owns it

The deletion half of this file is DONE and deleted with it (`DELETE /v1/warehouses/{id}` +
`DELETE /v1/projects/{id}` shipped 2026-08-04, 54fc413; the design that drove them was
`open_hierarchy_lifecycle.md`, now retired). What remains is the half nothing owns: **reclaiming
bytes, and deciding when any of it runs.**

Everything below §2 was revised 2026-08-04 after reading the Lance table-format specs end to end
(`format/table/*`, the index specs, the performance guide, and the data-evolution guide). Several
claims this file previously made were wrong, and one shipped detector is wrong — see §3.

**Corrected again 2026-08-04 (#94), after an audit of this file itself.** §2's headline said
*"nothing reclaims yet"*. That is FALSE — compaction and version cleanup delete bytes on every tick
of the default chart — and it is the most dangerous kind of wrong, because it reads as "nothing
here is dangerous". §1's test column, §4's "still unowned", §6 and §7 were stale in the safer
direction. Corrections are marked in place. `open_lakehouse_diff.md` was corrected in the same
round (#95).

## 1. `services/maintenance` — what it is

Renamed from `services/compaction` (06cc757) because it does four things and "compaction" named one.
**Not** `garbage-collector` — compaction and reindex are not GC. **Not** split into
`purge`/`prune`/`compact`/`reindex` services, because the operations are ONE ORDERED PASS over one
dataset (`maintenance.services.optimize.compact_one`):

| Operation | Implementation | Tested |
| --- | --- | --- |
| **compaction** | `ds.optimize.compact_files(defer_index_remap=True)`, falling back to plain compaction when the dataset has no `row_addrs` | ✅ unit, over real Lance datasets on a LOCAL dir — not object storage (§6) |
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

## 2. The reclamation gap — what DELETES today, and what only reports

> **Corrected 2026-08-04 (#94).** This section was headed *"nothing reclaims yet"*, which is false
> and was the most dangerous sentence in the file: it reads as *"nothing here is dangerous"*, and a
> recorded stance like that stops people checking. The same failure mode as COVERAGE.md's
> soft-delete "N/A".
>
> **Two operations DELETE BYTES on every tick of the default chart.** `compact_one` calls
> `ds.optimize.compact_files()` (`optimize.py:118,129`) and `ds.cleanup_old_versions()`
> (`optimize.py:189`), driven by the `maintenance-cron` Dapr binding, which `chart/values.yaml`
> ships **enabled at `@every 120s`**. Per-table GC also runs on demand from the UI
> (`catalog/services/maintenance.py`, `TableDetail.svelte:551`). Compaction rewrites data files and
> drops the originals; cleanup removes superseded manifests and their unreferenced files.
>
> What is genuinely report-only is narrower and worth naming exactly: **the orphan-FILE pass**
> (`orphans.py`, and it is the one that is KNOWN WRONG — §3) and **trash expiry** (#75). Those two
> delete nothing and stay that way until #79's gate opens.
>
> The protections that make the deleting half safe are real, and they are what this section should
> have been claiming credit for instead: tags are EXEMPT from cleanup, `delete_unverified` is never
> passed, `older_than`/`retain_versions` bound what is eligible, and since #93 the read itself is
> bounded so the sweep cannot OOM the pod mid-pass.

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

**Now expressible (d8a4868).** `auto_cleanup_interval_commits` on the policy hands version
reclamation to the DATASET and SKIPS our cleanup step — one owner, never two, because both running
is two processes racing to delete the same manifests. `DatasetResult.auto_cleanup_configured`
reports which owner ran, since "reclaimed nothing" and "the writer owns this one" otherwise both
read as `old_versions_removed=0`. It stays opt-in: auto-cleanup runs inside the commit, needs delete
permission and adds latency to every Nth write, so a rarely-written tier is better served by the
sweep, which costs the writer nothing.

**Also now expressible: `scan_batch_size`.** Lance's default read batch is 8192 ROWS, and rows are
not a unit of memory — against ~1.8 MB bronze page rows that is ~15 GB per compute thread. Safe for
feature tables, ruinous for a blob tier, so it is per-tier policy rather than a constant.

**And a global bound (#93, ac16876).** `scan_batch_size` shipped as policy-only with no default,
so the UNPOLICIED estate — what `helm install` produces — ran at Lance's 8192-row batch across a
thread per HOST core, in a pod on a 512Mi tier, every 120s. `MAINTENANCE_SCAN_BATCH_SIZE=64` +
`MAINTENANCE_COMPACT_THREADS=2` now bound it (~230 MB); a policy still overrides per tier. Both had
to move because the memory is their PRODUCT.

**Corrected 2026-08-04 (#94): "still unowned: WHICH / HOW OFTEN / WHICH warehouses" — all three
shipped.** WHICH is the per-step `compact_enabled` / `cleanup_enabled` / `optimize_indices_enabled`
flags; HOW OFTEN is `compact_interval_hours` plus the per-dataset cadence stamp (7ea481c); WHICH
warehouses is #81 — the sweep reads the warehouse REGISTRY and covers every provisioned bucket, not
just the configured list.

What is actually left of #65 is narrower than "the fields exist, the surface does not": the
**project-scoped policy API shipped too** (`policies.py:188-271`, `resolve_policy`'s project branch,
routes in the generated client). Only the UI to set it per project/tier is missing.

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
## 5b. The last two specs, MINED (0dc737c) — a fifth false-positive class

`mem_wal.md` and `data_overlay_file.md` were the two unread specs. Both describe files the orphan
pass had never seen, and in the directory where a false positive costs real values. Findings:

**`mem_wal.md` — `_mem_wal/`.** MemWAL is an LSM layer: each shard holds a write-ahead log plus
SSTable datasets, and **no base-table manifest references any of it**. To the shipped detector that
is the definition of garbage — the whole tree would be reported. Two further facts make reclaiming
there a job for the shard manifests, not a prefix subtraction:

- **SSTable compaction has an ORDER.** Newer SSTables shadow older ones, so "unreferenced" is only
  meaningful relative to a shard's own manifest, not the table's.
- **Deleting WAL files WEAKENS WRITER FENCING.** Fencing detects a stalled writer by a
  put-if-not-exists COLLISION — the very file whose existence is the signal. GC the file and the
  collision cannot happen, so a zombie writer is no longer fenced. This is the rare case where
  correct-looking reclamation silently removes a SAFETY mechanism rather than bytes.

**`data_overlay_file.md` — flag 64.** Overlays are `data/overlay-*.lance` referenced from
`DataFragment.overlays`, which `data_files()` does not enumerate — so every overlay reads as
unreferenced. Two invariants also constrain any future folding we do:

- **overlay→overlay merges must be CONTIGUOUS in `committed_version`.** Merging across a gap is not
  a slower path, it is wrong.
- **an overlay→base fold on an INDEXED field must rebuild that index in the SAME commit,** or the
  index silently describes pre-fold values.

The spec's own rule settles the design: a reader that does not understand flag 64 **must refuse the
dataset** — "a correctness bug rather than a degraded experience." So the pass REFUSES both layouts
(unavailable-with-reason) rather than guessing, which is what 0dc737c implements and what
`test_a_memwal_shard_tree_is_refused` / `test_a_dataset_using_overlays_is_refused` pin.

## 6. Still not verified

- **Orphan RECLAMATION and TRASH PURGE** — reports exist; neither deletes, deliberately (§2).
  Compaction and version cleanup DO delete, and always have — see §2's correction.
- **Reindex from scratch** — §5.
- **The sweep against real object storage** — sweep tests use local dirs; only the skipped e2e
  touches S3. (The reconcile + orphan passes HAVE been run against the live rustfs.)
- **Multi-warehouse maintenance** — §4.

## 7. Order of work

1. ~~Rename the service to `maintenance`.~~ Done (06cc757).
2. ~~Wire the reconciler so it can RUN.~~ Done (04129b6).
3. ~~The orphan-file detector, report-only.~~ Done (3dd3e13) — **but see §3, it is wrong for
   branches and clones and must be gated before anyone trusts it.**
4. ~~Policy surface, including the auto-cleanup decision in §4.~~ Substantially done — per-step
   flags, cadence, retention, project tier, `scan_batch_size`, `auto_cleanup_interval_commits`. The
   UI to set them per project/tier is what remains (#65).
5. ~~Multi-warehouse sweep coverage.~~ Done (#81) — the sweep reads the warehouse REGISTRY, so a
   bucket provisioned by an API call after the last config edit is swept. **Residual: the orphan
   scan at `reconcile.py:613` still iterates `settings.sweep_buckets` and did not get the same
   treatment.**
6. ~~Global `scan_batch_size` floor.~~ Done (#93, ac16876) — see §4.

Re-ordered 2026-08-04 (#94) from here, by the audits:

7. **Bytes reclaimed** in `summarize` + a metric + a control event per reclaiming sweep. Today a
   sweep that deleted a terabyte and one that deleted nothing produce the same shaped report, which
   is a poor property for the half of this service that DOES delete (§2).
8. **A per-tick budget + rotated bucket order.** The sweep walks every bucket every tick; at estate
   scale the last bucket is maintained only if the tick has time left, and nothing says which.
9. **A chart toggle for the orphan scan** (`MAINTENANCE_ORPHAN_SCAN_ENABLED` is env-only today).
10. **TRASH PURGE BEFORE ORPHAN RECLAMATION.** If exactly one reclaimer is to earn its delete
    permission first, it should be this one: a bounded delete of a RECORDED path beats an inference
    from prefix subtraction, and the sizes agree — the live estate's orphans are 23 `.txn` files
    (kilobytes), while a dropped table's bytes are ~1.8 MB/row. The orphan pass is also the one that
    is known wrong (§3).
11. Only then consider letting the orphan pass delete.

Tracked as tasks #51, #55, #57–#65, #79, #80.

---

Delete this file when the work lands.
