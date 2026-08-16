# open: table maintenance — what is left

Worked down 2026-08-16. Everything on the original nine-item list is closed and deployed; **one item
is implemented but cannot be WITNESSED in this estate**, and that is the only reason this file still
exists.

**Delete this file when the list is empty.**

---

## T6 — implemented on both sides, unwitnessed, and structurally so

The chain is complete and live on `main-646849c5`:

- **Read half** (`70675e5e`): `maintenance.core.lineage_emit.declared_table_id` reads
  `lineage.dataset_id` off the dataset, and `sweep.py` prefers it over the URI derivation.
- **Write half** (`1a672a45`): both movers already stamped it; the cascade HEAD did not —
  `produce.py` called `seed_bronze(bronze_uri, storage_options())` and dropped the id, so the one
  dataset that starts every cascade was the only unstamped tier. Now computed once as
  `bronze_dataset_id` and used for BOTH the event's `output_name` and the on-disk stamp.
  Guarded by `test_medallion_cascade.py`, which fails on bronze without it.

**What is not proven: no medallion tier has been observed emitting a maintenance Run node in AGE.**
The read half IS witnessed end-to-end — `silver$emitproof` carries one — but no *medallion* dataset
does, and in a fresh estate none can:

- `sweep.py::_did_material_work` gates the emit on `fragments_removed or old_versions_removed`.
  Correct by design: a 120s cron would otherwise flood the graph with no-op compaction runs.
- **Compaction can never have work here.** Every cascade stage writes `mode="overwrite"`
  (`compute.py:216`, `compute.py:288`), which replaces the fragment set rather than appending, so
  fragments never accumulate toward `BRONZE_TARGET_ROWS` (512).
- **Version cleanup cannot fire for a week.** `MAINTENANCE_OLDER_THAN_DAYS` defaults to 7 and is
  `ge=1`, so a version written today is not reclaimable today, and 0 is not expressible.
- **The one narrow override is unreachable — and this is the real finding.** A retention policy WOULD
  force it: `set_namespace_policy` builds its path from `settings.root`, `resolve_policy` matches a
  namespace record by directory prefix (`rel.startswith(path + "/")`), so a record on `medallion`
  governs `medallion/bronze`; and `retain_versions` alone sets `effective_older_than = None` — keep-
  last-N with no age bound, sidestepping the 7-day wall. Measured live with a real dex bearer:

      POST /v1/namespace/medallion/policy/set   -> 403 can_delete required on namespace:medallion
      POST /v1/table/bronze$events/policy/set   -> 403 can_drop required on table:bronze$events

  Both 403 rather than 404, because the gate runs before existence resolution — and neither object
  exists. `medallion` is not a catalog namespace at all (it is absent even from the reconciler's
  `unbound_namespaces`, which lists `bronze`, `transcripts_v2`, `acme-bronze/silver/gold`), and
  `bronze$events` is not a registered table.

**So the medallion tiers are DATA WITHOUT GOVERNANCE.** With no namespace record, no table record and
no parent tuple, no principal can hold `can_delete`/`can_drop` over them — which means no policy, no
protection and no grant can ever apply to the datasets the cascade writes. T6's verification is
blocked behind that, not behind a timer. Making it possible means creating governance records for live
data — the same "bind a legacy namespace" decision `unbound_namespaces` needs, and a governance change
rather than a verification step. It should be decided on its own merits, not to make a check pass.

*Blocked on: a governance decision (bind the medallion path), or 7 days of elapsed time. Nothing to
implement in the maintenance service itself.*

---

## Closed and deployed

- **T1** `ce57bb67` · **T2** `5657a164` · **T3** `4c8b9e31` · **T4** `18615b13` · **T5** `15c74e2d` ·
  **T7** `93021817` · **T9** `e4e73b68` · **index_columns** `2f85b270`
- **T8** `a1c89bc8` — `orphaned_annotation_tasks` (3) and `unbound_namespaces` (5) no longer block
  certification: excluded via `NON_GATING_CATEGORIES` with the reasoning at the call site, because no
  door in the product can clear either. Live reconcile totals 67 against a raw sum of 75.
- **Multi-base leak** `1b279641` — upstream-blocked, recorded durably in
  `.claude/skills/rask-lance-catalog/SKILL.md` with the `EXTRA_BASE_DELETED = []` measurement.
- **The trash-record rule** — recorded in the same skill after I violated it: diagnosing a
  `versions_removed: 0` count by hand-running `cleanup_old_versions` on a dataset without checking its
  `_trash/` record destroyed time-travel to v1–v4 on a still-restorable object. Tags are not the
  check; the trash record is.

**First wave, same day:** tier sizing dead for every governed tier · the sweep's summary reaching no
log sink (`obs.py` still named `compaction`) · policy leaked on drop and lost on rename · the
reconciler refusing to resolve its FGA store when unpinned · GreptimeDB OOMKilled ×8 · the chart's own
observability bucket reported as drift forever · the ray-batch e2e leaking 32 datasets into the
governed bucket · lock/replica coupling unguarded · no maintenance dashboard · `lineageEmit` and
`orphanScan` off · 9 ghost governance tuples · `k3s-pins.sh` destroying the pins its own guard refused
to overwrite.
