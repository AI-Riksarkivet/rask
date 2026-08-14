# open-table-maintenance — what is LEFT after the 2026-08-05 wave

**Status: HANDOFF.** The original plan doc of this name was retired on 2026-08-05 (`b15af481`) once its
core landed; this file is the *residue*, rebuilt 2026-08-14 for a session picking the work up cold.
It also restores the pointer `.claude/skills/rask-lance-catalog/SKILL.md:102` still makes to this
filename — that pointer has been dangling since the retirement.

**What DID land (do not re-do):** reclamation live behind a clean drift report (#79), the sweep against
real object storage (#80), the multi-warehouse sweep over every registry bucket (#81), the global
`scan_batch_size` floor (#93), one shared bounded Lance cache session (#102), feature-flag refusals for
flags 16/64 (#64), orphan-detector correctness for branches / shallow clones / multi-base / blob
sidecars (#57), Lance auto-cleanup config (#58), `defer_index_remap` + Fragment Reuse Index (#59), the
policy surface (#51) and its project-scoped view (#65).

## How to read the status tags

Every claim below was re-verified against the working tree on **2026-08-14**. The tags are the point of
this document — the underlying notes are 9 days old and three of them have already drifted.

| tag | meaning |
| --- | --- |
| **STANDS** | re-read the code today; the defect is still there |
| **MOVED** | something changed since the note was written — *verify before implementing*, the note may be describing a fixed world |
| **HANDS OFF** | another session is actively editing these files right now |

---

## 1 · #128 — six undisclosed scope cuts (the one with real safety in it)

### 1a · The orphan scan is production-dark, and a purge can certify an estate it never looked at — **STANDS**

`services/maintenance/src/maintenance/core/config.py:160` —
`orphan_scan_enabled: bool = Field(default=False, alias="MAINTENANCE_ORPHAN_SCAN_ENABLED")`, and
`grep orphan chart/values.yaml chart/templates/maintenance.yaml` finds **no lever** (only an unrelated
comment and a `--cascade=orphan`). So every S3-layout refusal class the #57/#64 work built is switched
off in production and cannot be switched on from the chart.

Worse in combination: `report_is_clean` does not block on the *skip*. Reclamation is gated on a clean
drift report (#79), so with the scan dark the estate can pass that gate having never inspected its file
layer. **This is the highest-value item in the whole list** — it is the difference between "we looked
and it was clean" and "we did not look".

Fix shape: add the chart lever; make a skipped orphan scan a NON-clean report (or an explicit,
loudly-recorded `IncompleteScan`) rather than an absence.

### 1b · The sweep half's unit coverage — **MOVED, verify first**

The note claimed `sweep.py:135-137` never executes in the unit suite while the orphan half had four
tests, both presented as landed. **`tests/unit/test_maintenance_sweep.py` now exists.** Its existence
does not prove those lines execute — check coverage of the sweep path specifically before writing new
tests, and if it is genuinely covered now, close this sub-item rather than padding it.

### 1c · Silent depth bound — **MOVED, partially addressed**

`services/maintenance/src/maintenance/services/optimize.py:68` still reads
`discover_datasets(fs, bucket, *, max_depth: int = 3)`, so a dataset deeper than 3 is still neither
maintained nor scanned. **But the silence is gone**: `:64` now carries
`#: Prefixes the walk stopped at because it hit max_depth — each may hide any number of datasets.` and
`:56` documents the hazard, so truncation is now recorded in a `Discovery`. Remaining question for the
implementer: is that truncation *surfaced* into the report / does it make a report non-clean, or is it
recorded and dropped? Verify before acting. (Note the function was RENAMED from `discover_dataset_uris`
to `discover_datasets` — the older notes and OPEN-WORK use the old name.)

### 1d · `purge.delete_location` can destroy a live clone's base — **STANDS (finish the read)**

`services/maintenance/src/maintenance/services/purge.py:313` `delete_location(...)` deletes the recorded
dataset directory wholesale; its docstring covers idempotency and byte accounting but says nothing about
`base_paths`. A shallow clone resolves its data through the SOURCE's directory (feature flag 16), so
purging a source that another table still clones from destroys the clone. Directly related to item 3
(#114) — same hazard class, different trigger (purge vs compaction). Read the full function before
implementing; the fix wants the same "collect foreign base_uris first" pre-pass #114 needs.

### 1e · `maintenance/core/lance_trace.py` — **HANDS OFF**

216 lines, no caller, no test — *and it is UNTRACKED in git*, alongside a modified `lineage_emit.py`.
That is another session's in-flight #62 work, not dead code. **Do not delete it, do not "clean it up".**
Coordinate with whoever owns #62 (item 5 below).

### 1f · `DatasetResult.bytes_removed` populated and never read — **MOVED, likely refuted**

`optimize.py:41` declares it, `:253` sets it. The note said nothing reads it — but
`packages/ratch/src/ratch/cli/media.py:231` prints `stats.bytes_removed`. **Careful:** that is the Lance
cleanup stats object, not `DatasetResult`. Confirm which one the claim is about before deleting a field
someone's CLI is printing.

### 1g · Six falsified comments

A wrong test filename, a `compactionReplicas` value that does not exist, a "prod sets false" claim about
docs endpoints that actually ship ON, and a pointer to the deleted `open_table_maintenance.md` (this
file's own name — the skill pointer at `.claude/skills/rask-lance-catalog/SKILL.md:102` is the live one;
restoring this document fixes it). Estate rule: fix the skill in the same commit as the code.

---

## 2 · #60 — reindex-from-scratch: what `optimize_indices` cannot do — **STANDS**

`optimize_indices()` folds NEW fragments into EXISTING indices. Per the Lance specs it cannot handle:

- a changed index **type or parameters** (IVF_PQ → IVF_HNSW_SQ, `num_partitions`, `num_sub_vectors`,
  `nbits`, RQ `num_bits`) — the on-disk auxiliary schema itself differs per quantizer;
- `alter_columns` casting a vector column: *"If the column has an index, the index will be dropped if
  the column type is changed"* — so a cast **silently leaves the column unindexed**;
- FTS partition count, fixed at build time by `LANCE_FTS_TARGET_SIZE` — more partitions means slower
  queries and only a rebuild reduces them;
- an index whose `fragment_bitmap` no longer covers most fragments.

Wanted: **detect and REPORT, never auto-rebuild** — index coverage vs total fragments, index
type/params vs the configured target, and columns that lost an index to a cast. Rebuild stays an
explicit operator action.

## 3 · #114 — compacting a shallow clone's SOURCE breaks the clone — **STANDS**

Reproduced by the #64 agent: compacting a clone's source went 8 data files → 1, and the clone then
failed to open in a fresh process (`Not found: src/data/*.lance`). **Same-process reads LIE** via the
cache and still return rows, which is why this hides. The source's manifest carries no flag, so no
per-dataset check can detect it.

Fix shape: a per-tick **pre-pass** over the swept buckets collecting foreign `base_uri`s from every
flag-16 dataset, then refusing maintenance on those sources too. Builds on
`maintenance/core/features.py`. State the honest bound loudly: a clone living in a bucket the sweep
cannot see leaves its source exposed regardless. Pairs with 1d.

## 4 · #61 — fragment sizing + conflict policy — **STANDS (a decision, not a bug)**

`optimize.py:109` takes `target_rows_per_fragment: int | None = None` and `:171-172` passes it only when
set (None → Lance default sizing). Nobody has ever chosen this value against the real tradeoff:

- manifest-level work scales with fragment **count** (every write rewrites the manifest; opening,
  scan-planning and dataset-level conflict resolution all walk it);
- fragment-level work scales with fragment **size** — and **conflict detection is per-fragment**.

Lance's guidance: ~1M rows/fragment works to ~1B rows, then move toward ~100M; tens of thousands of
fragments per table is fine; keep a fragment well under object-store limits (10–100 GB reasonable, 1 TB
hard ceiling). And decisively: *"If you run many concurrent updates, deletes, or merge_insert
operations, err toward MORE fragments — conflict detection is per-fragment, so too few fragments leads
to excess retries."*

The annotator plane does exactly that (concurrent per-item writes), so medallion tables may want
more/smaller fragments than the sweep's target. **Decide per tier** — bronze page images are huge rows,
silver/gold features are not — and make it policy-driven through #51.

## 5 · #62 — maintenance observability (LANCE tracing → Greptime) — **HANDS OFF, in flight**

Narrowed 2026-08-05; the cache half already landed as #102. Remaining: (a) lance throttle metrics
**already ship** via `instrument_lance_if_available` — do NOT rebuild them; (b) the missing signals are
`lance::execution` (iops, bytes_read, indices_loaded) and `lance::io_events` (emitted only on
index-cache MISS — exactly what judges the #102 fix in prod); (c) `LANCE_LOG` alone **never** reaches
GreptimeDB on maintenance pods, because the otel-collector's `drop_app_file_logs` filter drops
file-tailed logs from `lance.dev/logs:otlp` pods. The honest route is
`lance.tracing.capture_trace_events` in-process, filtering **in the callback** (LANCE_LOG target filters
do NOT filter the callback — verified), re-emitted as OTLP through the existing `setup_otel` pipeline.

See 1e: `lance_trace.py` is the uncommitted start of exactly this.

---

## Suggested order

1. **1a** — the safety hole; small, and it makes every later report trustworthy.
2. **1d + 3 (#114)** — one pre-pass fixes both; do them together or the second re-opens the first.
3. **1b, 1c, 1f** — verification passes that will either close cheaply or reveal the real remaining gap.
4. **#60** — detect-and-report only.
5. **#61** — needs a per-tier decision; land as policy.
6. **#62 / 1e** — only after coordinating with the session already inside those files.

## Two traps for whoever takes this

- **The worktree at `/home/blackwell/Desktop/rask` is SHARED between live sessions.** Never
  `git add -A`; commit named paths only. `origin/main` moves under you.
- **`bare kubectl` reaches a stale kind cluster.** The live release is k3s —
  `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` (the Makefile already does this).
