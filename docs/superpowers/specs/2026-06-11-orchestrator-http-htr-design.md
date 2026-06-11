# Orchestrator-driven HTR via a lightweight HTTP job

**Date:** 2026-06-11
**Status:** Approved (design)

## Problem

The viewer's orchestrator is the intended background process for running batches:
each tick it reconciles S3, then submits the next prefetch / HTR chunk as a Ray
job running the rask `runner`. That path was built for the original local setup
and does not work against the dev-kuberay cluster + the standalone `/htr`
Serve endpoint we deployed:

1. **Serve app mismatch** — the `htr` pipeline calls `serve.get_app_handle("transcribe")`
   (and `htrflow` → `"htrflow"`). The cluster runs a standalone `htr` app
   (`POST /htr/transcribe`); neither handle-based app exists there.
2. **Ray version** — `uv run … runner` pins ray `<2.56` and `ray.init(address="auto")`
   into a ray `3.0.0.dev0` cluster; major-version mismatch.
3. **S3 write** — output goes to `images-batch-alto`, where `ser_devai_rw` is
   currently `AccessDenied`.

## Goal

Make the orchestrator run real HTR batches on dev-kuberay against the deployed
`/htr` endpoint, without porting the runner/Ray Data pipeline to ray 3.x.

## Approach (chosen)

A **lightweight HTTP job**: the orchestrator submits a self-contained script
that reads a chunk's pages from S3, POSTs them to `/htr/transcribe`, and writes
ALTO back to S3. No Ray Data, no rask-runner venv, no ray version port — the GPU
work stays in the already-deployed `/htr` replicas; the job is pure I/O.

Rejected alternatives: (B) port runner/htr/storage to ray 3.x + deploy the
handle-based Serve app — large, risky major-version port; (C) hybrid runner
shell + HTTP HTR — still carries most of B's ray-3.x port risk.

## Components

### 1. `components/scripts/htr_chunk_job.py` (new)

Self-contained job script, runs on the `rayproject/ray-llm:nightly-py312-cu130`
image (same as the sidecars). No imports from rask packages.

- **CLI args:**
  - `--cache-bucket` (input image bucket, e.g. `images-batch`)
  - `--output` (`s3://images-batch-alto`)
  - `--endpoint` (default `http://localhost:8000/htr/transcribe`)
  - `--batch <id>` (repeatable)
  - `--concurrency` (default `8`)
- **S3 client:** built from env handed to the job — derived `AWS_ACCESS_KEY_ID`/
  `AWS_SECRET_ACCESS_KEY`, `HCP_ENDPOINT`, `HCP_INSECURE`. boto3 with
  `verify=False` when `HCP_INSECURE` is truthy. (No rask `storage` import.)
- **Per batch:** list `<cache-bucket>/<batch_id>/` keys ending `.jpg`. For each
  page:
  - **Resumable:** skip if `<output-bucket>/<batch_id>/<stem>.xml` already exists.
  - GET image bytes from S3 → POST bytes to `--endpoint` → write returned ALTO
    XML to `<output-bucket>/<batch_id>/<stem>.xml`.
- **Concurrency:** a bounded `ThreadPoolExecutor(--concurrency)` on the driver;
  head-side threads are sufficient (I/O-bound) and saturate the 4 GPU replicas.
- **Errors:** per-page try/except — log and skip a failed page; the job does not
  abort on a single failure. Exit 0 on completion.
- **Deps:** `boto3` (installed via the job `runtime_env`); HTTP via stdlib
  `urllib.request`.

### 2. `viewer/services/submission.py`

- `build_entrypoint` branches on the spec's `entrypoint_kind`:
  - `"runner"` (default, unchanged): `uv run --project projects/runner runner …`.
  - `"http"`: `python components/scripts/htr_chunk_job.py --cache-bucket … --output … --endpoint … --batch …`.
- `submit_chunk`'s `runtime_env` adds `pip: list(spec.pip)` when non-empty
  (HTTP spec → `["boto3"]`). `working_dir = repo_root` and the `AWS_*/HCP_*/IIIF_*/RASK_*`
  env passthrough are unchanged.

### 3. `viewer/models/pipelines.py`

- `PipelineSpec` gains two fields: `entrypoint_kind: Literal["runner", "http"] = "runner"`
  and `pip: tuple[str, ...] = ()`.
- Add `PIPELINE_SPECS["htr_http"]`: `slot=Slot.HTR`, `entrypoint_kind="http"`,
  `pip=("boto3",)`, `stages=()`, `tracks_rayjob_id=True`.

### 4. `viewer/core/config.py` + orchestrator loop

- Add `prefetch_enabled: bool = Field(default=True, alias="RASK_PREFETCH_ENABLED")`.
- The loop skips the prefetch-submission block when `not settings.prefetch_enabled`.

### 5. Viewer `.env` (operational, not committed)

- `RASK_HTR_PIPELINE=htr_http`
- `RASK_PREFETCH_ENABLED=0`
- (optional) `RASK_HTR_ENDPOINT` if the default `localhost:8000` ever changes —
  not added unless needed.

## Data flow

```
orchestrator tick
  → S3 sync (existing reconcile_from_s3)
  → for each HTR-eligible chunk (cached ≥95%):
       submit_chunk → JobSubmissionClient.submit_job(
                         entrypoint = python htr_chunk_job.py …,
                         runtime_env = {working_dir: repo_root, pip: [boto3], env_vars: AWS_/HCP_/…})
  → job: list S3 pages → POST /htr/transcribe → write ALTO to images-batch-alto
  → next tick's S3 sync sees new .xml → transcribed_pages / htr_status advance
```

Progress is tracked by the **S3 reconcile** (authoritative), not in-job Ray
actor stats — so `stages=()` is correct; the UI shows the job running plus the
S3-derived page counts.

## Why no Ray in the job

The job driver is pure I/O (S3 + HTTP); GPU inference lives in the deployed
`/htr` replicas. Avoiding `ray.init` / Ray Data sidesteps the 2.55→3.x problem
and keeps the job runnable on the cluster image with only `boto3` added.

## Out of scope

- Prefetch on the cluster (disabled for now; images already in `images-batch`).
- Porting the runner / Ray Data pipeline to ray 3.x.
- The actor-per-stage progress UI (no per-stage actor stats from an HTTP job).

## Testing

- **Unit:** `build_entrypoint` for `entrypoint_kind="http"` produces the expected
  `python htr_chunk_job.py …` command; `runtime_env` includes `pip:["boto3"]`.
  Orchestrator loop skips prefetch when `prefetch_enabled=False`.
- **Live validation:** after the S3 write grant lands — set the env, start the
  orchestrator on a single HTR-eligible chunk, confirm ALTO files appear under
  `images-batch-alto/<batch_id>/` and the chunk's `htr_status` flips to `done`
  on the next sync.

## External dependency

Grant `ser_devai_rw` WRITE (and DELETE, for overwrites) on the
`images-batch-alto` HCP namespace. Until then the live validation step is blocked.
