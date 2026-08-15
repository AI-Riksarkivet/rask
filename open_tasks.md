# open_tasks — the pinned engineering task list

**What this is.** The five OPEN engineering tasks, in one place, because the live list (`/tasks`) dies
with the session. **Not** `TODO.md` — that is the product/frontend backlog (26 items: routes, sidebar,
Explorer, annotate, studio). This file is the backend/platform queue.

**This is an INDEX, not a copy.** Every item points at the document that owns it. A second full
statement of a task drifts from the first, and then nobody knows which one is true — the same defect
class as the stale `dataset_uri` helper and the drifted `read_blobs` docstring this session unpicked.
Read the source before starting; update the source, not this file, when the work moves.

Closed items are not listed. Git history is the record of those.

**Scope.** This file is the backend/platform queue. The wider estate queue — frontend (#110, #111,
#116, #130, #147), owner rulings (#98, #134, #143, #146), and the lakehouse/catalog items (#43, #48,
#56, #67, #84, #85, #91, #142) — lives in the session task list and in `open_lakehouse_diff2.md` §5.
`diff2` §5 row 2 (F2, time-boxed grants) is **half done**: the wrapper half landed 2026-08-14
(`b58eff4f` — `condition_context()` + `context` on all four read wrappers); the catalog call-site half
(`_require` passes no context) is still open.

---

## ✅ #148 — CLOSED 2026-08-15 in `3e816ffa`. Was never open long.

**The diagnosis was exactly right and the fix is already in.** `medallion.workflow` imports
`dapr.ext.workflow`; `services/medallion/pyproject.toml` did not declare it, the workspace venv
resolved it through a sibling (`flows`, `ingest`) so every test stayed green, and only the image's own
closure could see it — `.docker/rest-catalog.dockerfile`'s import gate failed with:

```
import gate FAILED — 1/238 modules could not be IMPORTED:
  medallion.workflow: ModuleNotFoundError: No module named 'dapr.ext.workflow'
```

Fixed the way the note prescribed: `"dapr-ext-workflow>=1.18"` added
(`services/medallion/pyproject.toml:30`, precedents `flows:19` / `ingest:22`) and the root `uv.lock`
refreshed (`medallion` entry carries both the dependency and the `>=1.18` specifier).

**Verified, not assumed** — `dagger call image --name=rest-catalog` → `EXIT=0`, gate passes 238/238.

Kept rather than deleted, because the note's *reasoning* is the durable part: a dependency satisfied
by a sibling in the shared workspace venv is invisible to every test and fails only in the image, and
`| tail -1` reports tail's status so a piped build prints success while pushing no tag. Confirm a tag
exists with `curl -s http://localhost:5000/v2/rest-catalog/tags/list` before deploying.

> **Note to whoever wrote the blocker.** It was authored against a tree that predated the fix by
> minutes. That is the same drift these five items are about — which is why the goal now runs
> verify-first before touching anything.

---

## The five — four CLOSED 2026-08-15, one still yours

| # | Task | State |
|---|---|---|
| 4 | `promotionReviewBand` value + Q6 scheduling | **OPEN — yours.** See below |
| 6 | #128a — the orphan scan is production-dark | ✅ `81af086f` |
| 7 | #128d + #114 — `base_paths` pre-pass, as ONE change | ✅ `81af086f` + `ccd296e3` |
| 8 | verify-first: three maintenance claims already drifted | ✅ verified; **two were wrong** |
| 9 | #60 + #61 — `optimize_indices` gaps, per-tier fragment sizing | ✅ `633ce5b7` |

Verified together: `4261 passed, 0 failed` with NATS reachable; ruff + `ty` clean.

**DEPLOYED AND VERIFIED LIVE 2026-08-15**, not merely built. `lance-rest-catalog:maint-728d2e41`
(`dagger call image --name=rest-catalog publish …`, `DAGGER_EXIT=0`, digest `sha256:43fa4d02…`), tag
confirmed present in the registry BEFORE the deploy, `kubectl set image deploy/rask-maintenance` →
rolled out, pod `2/2 Running`, 0 restarts. A REAL reconcile tick driven through the service's own Dapr
cron route returns the new field:

```json
"skipped": [{ "category": "orphan_files", "reason": "MAINTENANCE_ORPHAN_SCAN_ENABLED is off — …",
              "coverage_gap": true }]
```

and `report_is_clean` on that live report blocks. The estate currently blocks for TWO reasons — 14 real
drift findings (12 orphan buckets, 2 unbound namespaces) AND the unexamined file layer — with the
findings reported first, which is the designed ordering: drift is immediately actionable, a coverage
gap is a config change.

Two things the deploy itself surfaced, both worth knowing:

- **`dagger publish` failed first** with `http: server gave HTTP response to HTTPS client`. The build
  succeeded; only the push failed. `make dagger-engine` fixes it (the CLI had auto-provisioned a
  config-less engine), and the engine needs `_EXPERIMENTAL_DAGGER_RUNNER_HOST` exported.
- **The background wrapper reported "exit code 0" while the command exited 1.** Capture
  `DAGGER_EXIT=$?` explicitly — the same class of trap as `| tail -1` reporting tail's status.

**Still to deploy:** the running deployment predates the chart change, so it carries NO
`MAINTENANCE_ORPHAN_SCAN_ENABLED` env var at all. Rendering it needs `make k3s-up` (NOT a hand
`helm upgrade` — that replaces every deployed image with the chart default). Behaviour is correct
either way: absent reads as off, and off now blocks the purge instead of silently certifying.

**What #8 actually found, and it is why it ran first.** Two of the four claims did not survive
checking. `bytes_removed` hedged "possibly the Lance stats object" — it resolves AGAINST itself:
`DatasetResult.bytes_removed` is **write-only**, read by nothing. And the "#148 blocks every image
build" banner that headed this file was already fixed; a verifier rebuilt the image closure with
`--no-editable` and the gate passed 236 modules, `EXIT=0`.

**Three corrections to the issues themselves**, each measured rather than read:

- **#114 is not compaction-triggered.** `compact_files` ADDS the merged file and deletes nothing
  (4 → 5 files, clone still opens); `cleanup_old_versions` removes the originals (→ 1) and the clone
  fails `ArrowInvalid` **in a fresh process**. A guard placed where the title points is walked
  straight past, so the refusal sits in front of the whole compact→optimize→cleanup pass.
- **The #128d/#114 guard shipped DEAD.** `protected_roots` was defined, documented, tested — and
  called from nowhere. The sweep passed no `protected=`, the purge never built the refs. Every test
  passed because every test called it directly. Fixed in `ccd296e3` with an integration test that
  drives the real `run_sweep`.
- **Delta proliferation does not happen on pylance 9.0.0.** Measured over four append+optimize
  cycles: `num_indices` never leaves 1, the single delta absorbs the rows — it MERGES. The #60 check
  stays as a guard against a behaviour change and the test says so rather than faking a reproduction.

**#61's numbers are DEFAULTS, not tuning** — bronze 512 / silver 262144 / gold 524288 rows, derived
from the chart's working row widths (~1.8 MB bronze page images vs ~2 KB elsewhere), not from a
profile of production data. A #50 policy record still wins, so retuning is a config change.

---

### #4 — the review band. YOURS, and the only one I cannot decide.

Three of the four "owner input needed" questions in `open_medallion_workflow.md` were answerable from
precedent and are answered there. This one is not: how far a row-count delta may drift before a human
is asked is an ARCHIVAL policy call, not an engineering one.

Proposed default is **±25%**, plus "first promotion of this dataset" (the case where nobody has ever
looked, firing once per dataset rather than per run). It is a values knob either way, so tightening it
later is a config change and not a deploy.

Nothing is blocked on this: S1 shipped without the quality gate, which is S3.

### #6 — #128a, and you called this the one that matters.

`orphan_scan_enabled` defaults `False` and the chart ships no lever to turn it on. Combined with
`report_is_clean` not blocking on the skip, reclamation can pass its "clean drift report" gate over an
estate whose file layer was never inspected — the difference between *we looked and it was clean* and
*we didn't look*.

### #7 — #128d + #114, together or not at all.

`purge.delete_location` deletes a dataset directory with no `base_paths` check, so purging a source
destroys a live shallow clone. #114 is the compaction-triggered twin, already reproduced (8 data files
→ 1; the clone then fails to open in a fresh process — same-process reads lie via the cache, which is
why it hides). One "collect foreign `base_uris` first" pre-pass fixes both. **Doing either alone
re-opens the other.**

### #8 — verify BEFORE fixing; three claims have already moved.

- a sweep unit test now exists
- the depth bound now records its truncated prefixes instead of dropping them silently — and
  `discover_dataset_uris` was RENAMED to `discover_datasets`, so older notes and OPEN-WORK reference a
  name that no longer exists
- `bytes_removed` does have a reader, though possibly of the Lance stats object rather than
  `DatasetResult`

### #9 — #60 + #61. Genuine work, not defects.

- **#60**: detect and report what `optimize_indices` cannot do — changed index type/params, a cast
  silently dropping an index, FTS partition count, a stale `fragment_bitmap`
- **#61**: choose `target_rows_per_fragment` per tier, since conflict detection is per-fragment and
  the annotator writes concurrently

---

## HANDS-OFF while working #6–#9

`services/maintenance/src/maintenance/core/lance_trace.py` (untracked) and the modified
`lineage_emit.py` beside it are another session's in-flight **#62** work. Do not delete; coordinate.

---

## Two decisions still waiting on you

**`open_dapr.md` — not deleted, deliberately.** All 28 of its items are closed and the retirement
would follow the convention, but the file is referenced **72 times across 43 files**, including 30+
production sources, chart templates and `chart/alerting/rules.yml`. Those are load-bearing `§2.13` /
`§2.21` evidence citations. Deleting it orphans all 72 — the dangling-pointer problem that forced
`open_table_maintenance.md` to be restored. Either it stays as the evidence trail, or it goes and all
72 citations get rewritten to name the retiring commit. Say which.

**The S1 deploy needs a rollout restart.** `values.yaml` now scopes `lance-statestore` to app-id
`medallion`, and daprd cannot hot-reload an actor state store — a mover pod that started before that
change keeps the OLD scope list and will fail to dispatch on every delivery. `kubectl rollout restart`
the medallion deployments. The workflow itself is NEW, so no in-flight instances exist and **no drain
is required**.
