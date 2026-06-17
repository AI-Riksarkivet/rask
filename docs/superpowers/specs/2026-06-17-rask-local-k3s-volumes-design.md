# rask local k3s + generalize beyond IIIF — design

Date: 2026-06-17

**Status:** Approved (design), pending implementation plan.
**Strategy:** Two phases. Phase 1 generalizes input away from IIIF and pins a single
HTR endpoint — verifiable today on the Makefile Ray stack, no k3s. Phase 2 packages
the fleet + in-cluster MinIO/Postgres/Ray into a self-contained local k3s install.

## Goal

Run the whole `rask` HTR app on one low-resource machine in a **local single-node
k3s** cluster, with a **single GPU HTR endpoint**, and **generalize input so any
image volume can be ingested** instead of being tied to Riksarkivet IIIF manifests.

## Problem

Three things block this today:

1. **Input is coupled to IIIF.** The ingestion + enumeration path assumes a
   Riksarkivet IIIF manifest: `IIIFCachedSource` + `get_image_ids`
   (`packages/storage/src/storage/iiif.py`), `build_batches_db.py` (creates
   `batches` rows from a Riksarkivet CSV), and the orchestrator HTR-eligibility gate
   in `core/services/orchestrator/derive.py` (`ready_for_htr` requires `page_count`
   and gates on `cached/expected ≥ 0.95` — the IIIF→S3-prefetch model). The runner
   `--batch` path builds an `IIIFCachedSource`; `submission.build_entrypoint` always
   passes `--batch/--cache-bucket/--iiif-url`.

2. **The HTR fleet assumes multi-GPU.** `make serve-up-both` deploys two Serve apps
   (`/transcribe` + `/htrflow`) across a 2-GPU pool, alongside a third-GPU qwen LLM.
   Too much for a single-GPU box.

3. **The chart is stale and not self-contained.** `chart/` (and
   `.docker/viewer.dockerfile`) still reference the deleted `viewer` monolith
   (`components/services/viewer`, `viewer.app:app`, the migration path); the app is
   now the microservice fleet under `components/services/` (`gateway`, `core_api`,
   `search_api`, `volumes_api`, `ray_api`, `orchestrator`) over the shared `core`
   library. The chart deploys only viewer + frontend and assumes external Postgres,
   S3/MinIO, and KubeRay.

## What already works (and is reused)

- **The storage layer is already generic.** `storage.build_source`/`build_sink`
  (`packages/storage/src/storage/uri.py`) pick `S3Source`/`FSSource` from the URI
  scheme. `volumes_api` already serves images/ALTO through `build_source` — it is
  **not** IIIF-coupled. The runner already supports `--input s3://… --prefix … —
  pipeline htrflow` via `S3Source`.
- **One Serve app already fits "single endpoint":** `htrflow`
  (`components/apps/runner/src/runner/htrflow_service.py`) collapses
  region→line→TrOCR→ALTO into one deployment, sized purely by env
  (`RASK_SERVE_REPLICAS`, `RASK_SERVE_GPU_FRAC`).
- **`_classify` already tolerates arbitrary volumes** (`sync.py`: "when `expected`
  is unknown, any cache counts as full cache"), and `_fetch_chunk_batches` already
  filters `manifest_status==OK`.

## Convergent design

With in-cluster **MinIO**, "any volume" = upload arbitrary images to the input
bucket under a `<volume_id>/` prefix; **no IIIF**. The only missing code is (1) a
**volume-registration** step that indexes the prefix into `batches`, and (2) a
**submission branch** that emits the `--input/--prefix` runner invocation. Then the
*entire existing orchestrator → submit → htrflow → ALTO pipeline runs unchanged.*

### Decisions (locked with the user)

- In-cluster **MinIO** (mirrors prod two-bucket layout); **single-GPU** `htrflow`;
  **full working deploy**; **code-gen first, then infra**; add `make k3s-install`
  (sudo); GPU base image = open spike.
- `register_volume` is **indexing-only** (does not upload); reuses
  `manifest_status=OK` + a **global `RASK_SOURCE_MODE`** flag (no schema change, no
  migration); lives as a **`core_api` HTTP endpoint** + thin script; **one volume =
  one chunk** (`chunk_total=1`).
- Work branches off `main`. Commits carry no Claude/AI co-author trailer.

---

## Phase 1 — Generalize beyond IIIF + single HTR endpoint (no k3s)

### 1.1 Volume registration — index an already-uploaded S3 prefix into `batches`

- **New** `components/services/core/src/core/services/registration.py`:
  `register_volume(session, client, *, input_bucket, volume_id) -> Batch`. Lists
  `input_bucket/<volume_id>/` via `storage.iter_keys` (image suffixes), counts →
  `page_count`, **upserts** a `Batch` (`batch_id=volume_id`, `page_count=count`,
  `manifest_status=ManifestStatus.OK`, `chunk_total=1`, `chunk_id=max(chunk_id)+1`
  on insert / preserved on re-register). IIIF-only fields (`iiif_endpoint`,
  `manifest_error`) stay null. Re-register is idempotent (refreshes `page_count`).
- **New endpoint** in `core_api` (`core/api/v1/endpoints/batches.py`):
  `POST /api/v1/volumes/{volume_id}/register` → calls the service, returns
  `BatchPublic`. Keeps state changes on the HTTP surface (CLAUDE.md). Add a thin
  `components/scripts/register_volume.py` dev tool that POSTs to it.
- `chunk_total=1` ⇒ each volume is immediately a complete chunk;
  `chunks_with_progress` reports `expected_pages=page_count`, so after one S3 sync
  `cached==expected` → `ready_for_htr` fires. **No change to `derive.py`/`sync.py`.**

### 1.2 Source-mode switch — submission emits the non-IIIF runner invocation

- `service_kit/config.py`: add
  `source_mode: Literal["iiif","s3"] = Field("iiif", alias="RASK_SOURCE_MODE")`;
  add `input_uri` to `RunnerParams` + `runner_params()` (`f"s3://{cache_bucket}"`).
- `core/services/submission.py` `build_entrypoint`: when `source_mode=="s3"` and
  `entrypoint_kind=="runner"`, emit
  `runner --input {input_uri} --output {output} --prefix {batch_id}/ --pipeline {spec.name}`
  (one batch_id per chunk; **omit** `--batch/--cache-bucket/--iiif-url`). IIIF path
  unchanged for back-compat. Thread `source_mode` through `submit_chunk`.
- No runner change.

### 1.3 Single HTR endpoint config (env only)

Local/k3s env: `RASK_HTR_PIPELINE=htrflow`, `RASK_PREFETCH_PIPELINE=none` (HTR-only,
already supported via `PIPELINE_DISABLED`), `RASK_SERVE_REPLICAS=1`,
`RASK_SERVE_GPU_FRAC=1.0`. Deploy only the `htrflow` Serve app
(`deploy_serve.py up --app htrflow`). *(Optional, low priority: make `SHARDS`
env-driven so a 1-replica deploy doesn't over-shard.)*

### 1.4 Chart/dockerfile correctness fixes (verify with `helm lint`/`helm template`)

`chart/values.yaml`: parameterize hardcoded `RASK_IIIF_URL`; fix `migrations.command`
path `…/viewer` → `…/core`; add the **required** `RASK_VIEWER_INPUT`/
`RASK_VIEWER_OUTPUT` to the ConfigMap (latent boot crash — `Settings()` raises
without them); default `ingress.className` `nginx` → `""`/`traefik`; add
`RASK_SOURCE_MODE`/`RASK_HTR_PIPELINE`/`RASK_PREFETCH_PIPELINE`. Stop referencing the
deleted `viewer` module so templates render (full fleet templates land in Phase 2).

### Phase 1 verification

- Unit: `register_volume` against `moto[s3]` (existing dev dep) — rows / `page_count`
  / `manifest_status`. Unit: `build_entrypoint` s3-mode string (has `--input/--prefix`,
  no `--iiif-url`).
- End-to-end (no IIIF): `make ray-up` → `RASK_SERVE_REPLICAS=1 RASK_SERVE_GPU_FRAC=1.0
  deploy_serve.py up --app htrflow` → `runner --input <local image dir> --output
  /tmp/out --pipeline htrflow` → ALTO emitted (CPU fallback if GPU image not ready).
- `make check` + `pytest -m "not slow"` green; `helm lint`/`helm template` render.

---

## Phase 2 — Local k3s deploy (MinIO + Postgres + Ray head + GPU htrflow)

### 2.0 GPU image spike (first — gates everything GPU)

Find a working **aarch64 + CUDA torch** combo for the GB10 (sm_120) — `nvcr.io/nvidia/pytorch`
arm64 is the leading candidate. Validate standalone (`torch.cuda.is_available()`,
then load YOLO + TrOCR). If none works readily, **fall back to CPU-only htrflow** (a
slow but working deploy). Output: the base image tag for 2.2.

### 2.1 `make k3s-install` (sudo, one-time, idempotent)

Install k3s (bundled containerd + kubectl), helm, and the **NVIDIA k8s device-plugin
DaemonSet**; ensure the `nvidia` containerd runtime + `RuntimeClass` (needs
`nvidia-container-toolkit` on host). Verify `nvidia.com/gpu` advertised as `1`.

### 2.2 Dockerfiles

- **`.docker/ray.dockerfile`** (GPU): base = spike result; carries `ray[serve]` +
  `runner`/`htr`/`storage` + htrflow + `components/scripts/deploy_serve.py` +
  `htrflow_pipeline.yaml`; reused for the ray-head pod and the serve-deploy Job;
  `HF_HOME=/cache/hf`.
- **One Dockerfile per microservice** (CPU):
  `.docker/{gateway,core-api,search-api,volumes-api,ray-api,orchestrator}.dockerfile`,
  each its own multi-stage `python:3.13-slim` + `uv sync --package <svc>` +
  `uvicorn <module>:app`, following the (retired) `viewer.dockerfile` structure.
  Independent build + dependency set per service. Migration uses the `core-api` image.
- `frontend.dockerfile` unchanged.

### 2.3 Chart templates (new under `chart/templates/`)

- **MinIO**: secret (auto-gen creds, pinned across upgrades, also surfaced as
  `AWS_*`), StatefulSet (`server /data`, 50Gi PVC, health probe), service (:9000),
  buckets Job (post-install hook, `mc mb` the 3 buckets, wait-loop on health). App
  `HCP_ENDPOINT=http://<release>-minio:9000`, `HCP_INSECURE=true`.
- **Postgres**: secret (auto-gen pw + assembled `DATABASE_URL`), StatefulSet
  (`postgres:16`, 8Gi PVC, `pg_isready` probe), service.
- **Ray + Serve**: ray-head Deployment (ray image, `ray start --head --num-gpus=1
  --block`, `resources.limits.nvidia.com/gpu: 1`, `runtimeClassName: nvidia`,
  `/dev/shm` memory emptyDir ≥30% RAM, HF-cache PVC at `/cache/hf`,
  `RASK_SERVE_REPLICAS=1`/`RASK_SERVE_GPU_FRAC=1.0`/`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0`),
  ray-head Service (8265/8000/10001/6379), serve-deploy Job (post-install hook
  weight 10; `RAY_ADDRESS=ray://<release>-ray-head:10001`; wait on :8265 →
  `deploy_serve.py up --app htrflow`), HF-cache PVC (10Gi, `local-path`).
- **Fleet**: Deployment+Service per service — `gateway`(:8888, ingress target),
  `core-api`(:8801), `search-api`(:8802), `volumes-api`(:8803), `ray-api`(:8804),
  `orchestrator`(:8810, **replicas:1 + `strategy: Recreate`** singleton). `envFrom`
  ConfigMap+Secret, `/api/v1/health` probes, init-container Postgres/MinIO waits on
  `core-api`/`orchestrator`. Gateway gets inter-service URLs in the ConfigMap.
  `orchestrator` owns `RASK_ORCHESTRATOR_AUTOSTART` (default false).
- **Ingress**: route `/api` → `<release>-gateway:8888`; className default Traefik.

### 2.4 values.yaml

Add `minio`/`postgres`/`ray`/`gpu`/`htrflow` blocks + a `services.*` map. Drop the
**required** `existingSecret` gate (creds chart-generated); render the app Secret
from the minio+postgres secrets. Set `RASK_SOURCE_MODE=s3`,
`RASK_HTR_PIPELINE=htrflow`, `RASK_PREFETCH_PIPELINE=none`. Single-node sizing:
ray-head 8Gi/2cpu req · 16Gi lim · `nvidia.com/gpu:1`; MinIO/PG 256Mi·1Gi; fleet
64–128Mi each; ≥80Gi free disk.

### 2.5 Makefile targets

`k3s-build` (native arm64 `docker buildx`, one build per Dockerfile), `k3s-import`
(`docker save … | sudo k3s ctr images import -`; concrete `:dev` tags,
`pullPolicy: IfNotPresent`), `k3s-up` (`helm upgrade --install rask ./chart --wait`
→ `rollout status` gateway → print URL + `/etc/hosts` hint), `k3s-down` (+ a
`k3s-purge` for PVCs). Release **pinned to `rask`** so `<release>-ray-head` resolves.

### 2.6 Startup ordering

Helm hook-weights for one-shot Jobs (migration post-install with `pg_isready`
init-container → buckets → serve-deploy) + init-container wait-loops for
cross-resource readiness + `/api/v1/health` readiness probes. Orchestrator
autostart-off removes the Serve-before-submit hard edge.

### Phase 2 verification

`make k3s-install` → `make k3s-up` → all pods Ready, Jobs Complete, GPU scheduled on
ray-head; open `http://rask.local/`; upload images to MinIO `images-batch/<vol>/`,
`POST /api/v1/volumes/<vol>/register`, start the orchestrator, watch ALTO land in
`images-batch-alto/<vol>/` and render in the viewer.

## Non-goals (this design)

- KubeRay operator (hand-rolled single head suffices for one node).
- NATS JetStream orchestrator (stays the in-process singleton; pinned to 1 replica).
- In-cluster search/catalog indexing (one-shot scripts as today).
- Multi-volume-per-chunk in s3 source-mode (one-volume-per-chunk is enough now).
- Per-row `source_type` modeling (global `RASK_SOURCE_MODE` is enough; revisit only
  if mixed IIIF + S3 in one DB is ever needed).
