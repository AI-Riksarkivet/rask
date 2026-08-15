# Real Ray-cluster compute seam

The medallion cascade's in-process compute (`services/medallion/services/compute.py`) is the **fake-Ray**
placeholder — a synchronous `lance.write_dataset` that stands in for a distributed Ray Data job so the
event-driven loop is testable without a cluster. This doc covers the **real** thing: an actual Ray cluster in
kind + `ray job submit`, proving Lance's distributed capabilities against RustFS. Production is KubeRay (a
`RayCluster` CR) via the rask merge; this is the raw-cluster proof that the seam is real, not fake.

> Everything here is the Ray **DATA** shape (bounded stage transforms). The Ray **TRAIN** shape —
> long-running training jobs, model-as-Lance-dataset, `jobType=TRAINING` lineage — is a separate,
> decided design: see [`docs/RAY-TRAIN.md`](RAY-TRAIN.md) (task #115).

## What runs

| Piece | File |
| ----- | ---- |
| Thin CPU Ray image (`rayproject/ray:2.56.1-py312-cpu` + `lance-ray==0.5.0` + `pylance==9.0.0`) | `.docker/ray-lance.dockerfile` |
| A real Ray head (GCS 6379, dashboard/job API 8265) + Service, single node | `deploy/ray-lance-demo.yaml` |
| The submitted job — a genuine distributed Lance pipeline | `scripts/ray_lance_job.py` |
| One-shot driver | `make ray-demo` (and `make ray-demo-clean`) |

`make ray-demo` builds + `kind load`s the image (`make ray-image`), applies the head, waits for it, then
`ray job submit`s `ray_lance_job.py` (baked into the image at `/home/ray/jobs/`), passing a per-run `RUN`
via `--runtime-env-json` (exec-level env is NOT propagated to a Ray job). The job reads/writes
`s3://lance-catalog/ray-<run>/…` on the in-cluster RustFS. Tear down with `make ray-demo-clean`.

> **Lineage boundary (verified 2026-07-16).** `ray_lance_job.py` is a distributed-Lance *capability*
> proof — it writes throwaway `ray-<run>/` demo datasets and emits **no** OpenLineage by design (there
> is no governed dataset to attribute, and lineage on scratch data would be noise). Governed batch
> provenance lives in the medallion cascade's Ray stage path (`ray_stage_job.py`), which threads
> `source_rowid` and emits the `WROTE` edge exactly as the in-process compute does; that lineage was
> verified end-to-end (bronze→silver→gold connected, `source_rowid` present, `/reconcile` in_sync).
> The redeploy loop this target uses is the digest-verified pod-delete (not `rollout restart`) — a
> rebuilt same-tag image is asserted onto the running head before the job submits.

## What it proves (live, one job, four capabilities)

```
[1/4] WRITE   ok  fragments=4  dsv=2.2  stable_row_ids=True   # distributed append into a stable-row-id dataset
[2/4] INDEX   ok  indices=['id_idx']  query(id=7)->1 row
[3/4] EVOLVE  ok  [id,v,doubled] → [id,v,doubled,tripled]  v→v+1  (old version still pins the old schema)
[4/4] COMPACT ok  fragments 4->2                # compact_files merges the small fragments
RAY-LANCE ALL OK
```

- **Distributed WRITE + stable row ids** — `lance_ray.write_lance` has no `enable_stable_row_ids` param, so we
  create `dst` with stable ids (an empty table of the output schema) and then distributed-**append** the Ray
  fragments into it (`mode="append"`, `min_rows_per_file<max` → 4 fragments in parallel + one commit).
  Stable-row-ids is a dataset-level property, so the appended rows inherit it — `has_stable_row_ids=True`.
- **DATA EVOLUTION** — `lance_ray.add_columns` distributively adds `tripled = v*3`; the schema and version
  advance, and time-travel to the pre-evolution version still shows the old schema (immutable versions).
- **COMPACTION** — `lance_ray.compact_files(CompactionOptions(target_rows_per_fragment=…))` merges 4 → 2
  fragments.

## Verified lance_ray ↔ pylance version findings (grounded in reality, not just docs)

> **Re-measured 2026-07-28 under the R27 Ray-plane audit, after the image pins moved from
> `lance-ray 0.4.2` / `pylance 8.0.0` / `pyarrow 19.0.1` to the fleet's `0.5.0` / `9.0.0` / `24.0.0`.**
> The pins moved because a `pylance 8.0.0` reader **cannot read the blob-v2 datasets the fleet writes**
> — the full matrix is in [`architecture/lance-blob-v2-findings.md`](architecture/lance-blob-v2-findings.md).
> Findings 1–4 below are kept with their outcome at each version, because two of the three "verified"
> claims that were acted on turned out to be right for the wrong reason.

1. **Ray Data's built-in `write_lance` datasink** calls `write_fragments(storage_options_provider=…)`, a kwarg
   pylance 8.0.0 lacks → use the **`lance-ray` package** (`lance_ray.write_lance`), which is version-matched.
   Still the rule: the packaged integration is the supported seam.
2. **lance_ray's DISTRIBUTED scalar index build fails at 0.4.2 + pylance 8.0.0 — but not for the recorded
   reason.** The old note said pylance 8.0.0 "exposes neither" of `create_index_uncommitted(index_type=,
   fragment_ids=)` / `create_index_segment_builder`. Measured: **8.0.0 HAS both `index_type` and
   `fragment_ids`**, and the build still raises *"BTREE distributed indexing uses
   `create_index_uncommitted(..., index_type="BTREE", fragment_ids=...)`"*. So the conclusion (fall back to
   the native build) held while the stated cause did not. `scripts/ray_lance_job.py` no longer hardcodes the
   fallback: it ATTEMPTS the distributed build, prints which path produced the index, and degrades — so the
   capability job is the answer at whatever the image pins, not a note that ages.
3. `lance_ray.write_lance` genuinely had **no `enable_stable_row_ids`** at 0.4.2 (verified by signature);
   **it exists at 0.5.0.** The workaround — create the target with stable ids via native pylance
   `write_dataset(enable_stable_row_ids=True)` of an empty schema, then distributed-**append** the Ray
   fragments (the property is dataset-level) — is correct at BOTH versions, so the jobs keep it until a
   cluster run confirms the single-call form.
4. `compact_files` needs a real `CompactionOptions` on pylance 8 — a bare `None` trips an internal dict check.
5. **`lance_ray.read_lance` DOES surface `_rowid`** via `scanner_options={"with_row_id": True}` (verified at
   0.4.2 *and* 0.5.0), and 0.5.0 adds `with_metadata=True` for `_rowaddr` + a derived `_fragid`. The claim in
   `scripts/ray_stage_job.py` that it does not — which is why the tabular cascade head writes on the driver
   instead of distributing — was never measured and is false. Correcting the code is a live behaviour change
   to the cascade head and is recorded as a follow-up to prove on kind.
6. **0.5.0-only surface** (absent at 0.4.2, so any note predating the bump cannot have used it):
   `add_columns_from`, `merge_columns_from`, `vector_search`, `init_global_pool`/`set_global_pool`
   (reusable Ray Pool — upstream currently wires it to `vector_search` only), and `write_lance`'s
   `target_bases` / `external_blob_mode` / `allow_external_blob_outside_bases`.

## Event-driven cascade integration (wired, not just a demo)

The medallion movers can run their stage compute as a real `ray job submit` **in response to their Dapr/NATS
cascade trigger** — gated behind `medallion.ray` (default off; requires `medallion.compute` + a running Ray
cluster). Fake-Ray in-process stays the default.

- **Flag:** `MEDALLION_RAY_ENABLED` (+ `MEDALLION_RAY_ADDRESS`, default `http://ray-lance-head:8265`),
  chart value `medallion.ray`.
- **Submit path:** `services/medallion/services/ray_submit.py` — the mover POSTs to the **Ray Jobs REST API**
  (`/api/jobs/`) with `httpx` (no `ray` package in the mover image), passing `FROM_URI/TO_URI/STAGE` + S3
  creds as `runtime_env` env-vars, then polls `GET /api/jobs/{id}` under an `asyncio.timeout`. On success the
  mover `measure()`s the written dataset so the OpenLineage WROTE edge is **identical** to the in-process
  path; on failure/timeout it raises → the mover returns RETRY and Dapr redelivers (the job is
  overwrite-idempotent).
- **The job:** `scripts/ray_stage_job.py` (baked in the image) has **two paths**, chosen by whether the
  upstream carries a blob-v2 column:
  - **Tabular** → the distributed `lance_ray.read_lance → map_batches(stamp) → write_lance` path (Ray
    workers, one commit) at 2.2 + stable row ids.
  - **Media (blob-v2 present)** → a **pylance-native round-trip** on the driver: read the blob column via
    `read_blobs`, re-wrap with `blob_array`, derive an inline `thumbnail` + `embedding` from image
    payloads, and `write_dataset(2.2, enable_stable_row_ids=True)`. This is the SAME contract as the
    in-process `compute.transform_stage`; the deriver is inlined and **drift-pinned** to
    `services/medallion/services/media.py` by `tests/unit/test_ray_stage_job.py`.

### Why the media path is a pylance round-trip and not `lance_ray` end-to-end (Phase-3 parity, 2026-07-13)

`lance_ray` **is not the redundant layer here — it genuinely can't round-trip an inline blob column.**
Verified live, not assumed: `lance_ray.read_lance` materialises a `lance.blob.v2` column as plain
`large_binary` (the schema before/after: `extension<lance.blob.v2<BlobType>>` → `large_binary`), so a
`lance_ray.write_lance` of that Ray dataset writes plain binary and the column loses its blob typing.

**Still true at 0.5.0 (R27, 2026-07-28) — and it is deliberate upstream, not a gap.** `lance_ray`'s
datasource says so in the source: *"Blob v2 extension columns are exposed as plain LargeBinary bytes."*
The blob params (`base_store_params`, `initial_bases`, and 0.5.0's `target_bases` / `external_blob_mode` /
`allow_external_blob_outside_bases`) govern **external** `Blob.from_uri` references and where new data files
land — not inline round-trip typing. So the media path re-wraps via pylance — a justified bridge, not DIY
duplication. **Exit:** a lance_ray that preserves blob typing across read→write; drop the round-trip (and
the inlined deriver) then. The `thumbnail`/`embedding` derivation is our business logic and is never
something `lance_ray` provides — that stays regardless.

Worth copying from that same datasource: it handles the **null-blob sparse alignment** explicitly, comparing
the handle count from `take_blobs` against the scanned descriptor count and walking sparsely for blob v2
(*"Blob v2 currently skips null rows, returning fewer handles"*). Our own read path uses the simpler
cardinality-preserving route — `service_kit.lakehouse.blobs.read_aligned_table`, i.e.
`scanner(blob_handling="all_binary")` — for the same reason.

**Live-proven on kind (pre-R23 shape, kept as history):** (tabular) `/produce` → the then raw-to-bronze
mover (ray on) submitted a Ray job that produced `bronze` (`stage=bronze`, 2.2, `stable_row_ids=True`)
with real measured stats — that mover has since collapsed into the producer's bronze ingest head (R23:
the cascade's first Ray-backed hop is now bronze→silver). (media, 2026-07-13) `/ingest-media` (ray on) → the media stage ran
**as a Ray job** (no more `medallion_ray_blob_fallback`) and `silver-media` came back with `payload` still a
blob-v2 column **plus** derived `thumbnail` + `embedding`. With the flag off, `make e2e-medallion`
(fake-Ray path) still passes.

## Relationship to the cascade

This is the production shape of the `compute.py` seam: `read → transform → write → version` becomes a real
`ray job submit` instead of an in-process call. It also directly answers the "cascade doesn't parallelize"
limitation — Lance *does* parallelize writes (fragment-parallel + single commit); our in-process
`mode="overwrite"` was the placeholder, not a Lance limit. The mover-submits-Ray-jobs wiring above is done;
the **KubeRay operator** (a `RayCluster` CR) replacing the raw Ray head is the rask-merge step.
