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

## ⛔ BLOCKS EVERY IMAGE BUILD — ahead of the five

**#148 — `medallion.workflow` imports `dapr.ext.workflow`; `services/medallion/pyproject.toml` never
declares it.** The workspace venv resolves it through a sibling (`flows`, `ingest`), so every test
stays green; the image's own closure cannot, so `.docker/rest-catalog.dockerfile`'s import gate fails
and **no `rest-catalog` image can be built by anyone**. That one image serves catalog, lineage,
medallion, maintenance, viewer, search and annotator — so #6–#9 above cannot be SHIPPED until it lands,
even when their code is done.

Dagger prints only `✘ withExec … exit code: 1`; the module name comes from the gate's own recipe:

```
UV_PROJECT_ENVIRONMENT=/tmp/gatevenv uv sync --frozen --no-dev --package catalog --package lineage \
  --package medallion --package maintenance --package viewer --package search --package annotator
/tmp/gatevenv/bin/python .docker/import-gate.py catalog lineage medallion maintenance viewer search annotator
```

Fix: add `"dapr-ext-workflow>=1.18",` to medallion's dependencies (precedents `services/flows/pyproject.toml:19`,
`services/ingest/pyproject.toml:22`; `services/notifications:23` deliberately does NOT and says why),
then refresh the root `uv.lock`. Owner: whoever owns the medallion S1 work — it arrived in `0473e240`.

Symptom if ignored: the build "succeeds" through a pipe (`| tail -1` reports tail's status), the tag is
never pushed, and `kubectl set image` onto it leaves ErrImagePull. Confirm a tag exists with
`curl -s http://localhost:5000/v2/rest-catalog/tags/list` before deploying.

---

## The five

| # | Task | Owner | Source of truth |
|---|---|---|---|
| 4 | `promotionReviewBand` value + Q6 scheduling | **you** | `open_medallion_workflow.md` §9 |
| 6 | #128a — the orphan scan is production-dark | me | `open_table_maintenance.md` |
| 7 | #128d + #114 — `base_paths` pre-pass, as ONE change | me | `open_table_maintenance.md` |
| 8 | verify-first: three maintenance claims already drifted | me | `open_table_maintenance.md` |
| 9 | #60 + #61 — `optimize_indices` gaps, per-tier fragment sizing | me | `open_table_maintenance.md` |

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
