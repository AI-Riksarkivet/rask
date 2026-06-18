# rask — run the whole fleet on Docker Compose

**Date:** 2026-06-18
**Status:** Approved design
**Depends on:** Phase 1 (generalize beyond IIIF + single htrflow endpoint), branch `worktree-local-k3s-volumes`.

## Goal

Run the **entire** `rask` app on this machine with plain Docker — the full microservice fleet plus its infrastructure (MinIO, Postgres, Ray + htrflow Serve) — orchestrated by a single `docker-compose.yml`. This is the lighter-weight sibling of the k3s deploy (Phase 2): no k3s/helm/device-plugin install, `--gpus all` works directly. ~80% of the artifacts (per-service Dockerfiles, the GPU Ray image, bucket-init, migration ordering) are shared with, and reused by, the eventual k3s chart.

## Context (verified)

- **Service fleet** under `components/services/`, deployable compositions under `projects/`. Canonical port/module map (from `dev-micro.sh`):

  | Service | project | module | port |
  |---|---|---|---|
  | gateway | `gateway` | `gateway:app` | 8888 |
  | core-api | `core-api` | `core_api:app` | 8801 |
  | search-api | `search-api` | `search_api:app` | 8802 |
  | volumes-api | `volumes-api` | `volumes_api:app` | 8803 |
  | ray-api | `ray-api` | `ray_api:app` | 8804 |
  | orchestrator | `orchestrator` | `orchestrator:app` | 8810 |
  | runner | `runner` | (Ray job; no server) | — |

- **Existing Dockerfiles** in `.docker/`: `frontend.dockerfile` (+ `frontend.nginx.conf`) — keep; `runner.dockerfile` — GPU base, x86 `nvidia/cuda:12.4` — rework to arm64; `viewer.dockerfile` — stale (`viewer` module deleted) — retire, but it is the clean per-service template to copy.
- **Environment:** aarch64 + NVIDIA **GB10** (Grace-Blackwell, sm_120), driver 580 (CUDA 13). Docker 29.5.3 working; `nvidia` runtime registered; `--gpus all` passthrough **confirmed** (container saw `NVIDIA GB10 / CUDA 13.0` via `nvidia/cuda:13.0.1-base-ubuntu24.04`). torch 2.12.0+cu130 with CUDA works on the **host**; in-container torch on sm_120 is the one accepted open risk.
- **Repo currently has no docker-compose** (CLAUDE.md: "no docker-compose") — this is net-new.
- The Phase-1 code already makes "any S3 volume → single htrflow → ALTO" work: `register_volume` (`POST /api/v1/batches/{id}/register`), `RASK_SOURCE_MODE=s3` submission branch, htrflow single endpoint.

## Decisions (from user)

- **All services** containerized (full fleet, not a subset; not a hybrid host/compose split).
- **Per-service Dockerfiles on a shared base** (not one mega-image, not a single multi-target image). Independent dependency sets, independent restart/scale; reusable verbatim by k3s.
- **GPU-required, assume base** — wire `--gpus all` directly, assume an arm64 torch+CUDA base (NGC or our cu130 wheels) works. No standalone spike; risk surfaces at first real run, CPU fallback documented.
- **Orchestrator autostart OFF** — `RASK_ORCHESTRATOR_AUTOSTART=false`; processing is triggered manually (`POST /api/v1/orchestrator/start` or run the runner directly). Matches the chart default and sidesteps the Serve-before-submit ordering edge.
- Compose **coexists** with the Makefile / `dev-micro.sh` dev runbook — it does not replace them.
- Commits: no Claude/AI co-author trailer.

## Approach (chosen)

**docker-compose, per-service images on a shared base + a GPU `ray-head`.** Rejected alternatives: hybrid infra-in-compose/app-on-host (not "all services on docker"); one supervisord mega-container (no isolation, throwaway for k3s).

### 1. Topology

One `docker-compose.yml`; a single user-facing published port (`gateway` → host `:8888`). Internal service-name DNS for everything else.

| Container | Image | Command | Internal port |
|---|---|---|---|
| `gateway` | per-svc | `uvicorn gateway:app` | 8888 (published) |
| `core-api` | per-svc | `uvicorn core_api:app` | 8801 |
| `search-api` | per-svc | `uvicorn search_api:app` | 8802 |
| `volumes-api` | per-svc | `uvicorn volumes_api:app` | 8803 |
| `ray-api` | per-svc | `uvicorn ray_api:app` | 8804 |
| `orchestrator` | per-svc | `uvicorn orchestrator:app` | 8810 |
| `frontend` | `frontend.dockerfile` (exists) | nginx static SPA | 80 (behind gateway) |
| `ray-head` | `ray.dockerfile` (GPU) | `ray start --head --block` + `deploy_serve.py up --app htrflow` | 8265 / 8000 / 10001 / 6379 |
| `minio` | `minio/minio` | `server /data --console-address :9001` | 9000 (+9001 console) |
| `postgres` | `postgres:16` | — | 5432 |
| `migrate` *(one-shot)* | core-api image | `alembic upgrade head` | — |
| `minio-init` *(one-shot)* | `minio/mc` | `mc mb images-batch images-batch-alto` (idempotent) | — |

### 2. Images

- **7 per-service Dockerfiles** under `.docker/`: `gateway.dockerfile`, `core-api.dockerfile`, `search-api.dockerfile`, `volumes-api.dockerfile`, `ray-api.dockerfile`, `orchestrator.dockerfile`, plus the existing `frontend.dockerfile` (reused unchanged). Each is modeled on `viewer.dockerfile`: two-stage, `uv sync --frozen --no-install-workspace --package <svc>` then `uv sync --locked --package <svc> --no-editable`, copy `/opt/venv`, run `uvicorn <module>:app`. They differ only in the `--package` name, the `CMD` module, and `EXPOSE`/healthcheck port — so a shared `python:3.13-slim-bookworm` base + uv layer keeps total build cost ≈ one image under buildx layer caching. The stale `viewer.dockerfile` is deleted.
- **`ray.dockerfile`** = current `runner.dockerfile` with the `FROM` (both stages) swapped to arm64 `nvidia/cuda:13.0.1-runtime-ubuntu24.04`, relying on `uv.lock` to resolve cu130 torch for aarch64. Additionally carries `components/scripts/deploy_serve.py` + the htrflow pipeline config so the container can both run the Ray head and self-deploy the `htrflow` Serve app. Keeps `HF_HOME=/cache/hf` and the Ray shm/ulimit runtime requirements.

### 3. Config & wiring

A compose-level `.env` (or `environment:` blocks) supplies:

- `DATABASE_URL=postgresql+asyncpg://rask:rask@postgres:5432/rask`
- `HCP_ENDPOINT=http://minio:9000`, `HCP_INSECURE=true`, `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (= MinIO root creds), `AWS_REGION=us-east-1`
- `RAY_ADDRESS=ray://ray-head:10001`, `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0`
- Inter-service URLs for the gateway: `http://core-api:8801`, `http://search-api:8802`, `http://volumes-api:8803`, `http://ray-api:8804`
- Phase-1 flags: `RASK_SOURCE_MODE=s3`, `RASK_VIEWER_INPUT=s3://images-batch`, `RASK_VIEWER_OUTPUT=s3://images-batch-alto`, `RASK_HTR_PIPELINE=htrflow`, `RASK_PREFETCH_PIPELINE=none`, `RASK_SERVE_REPLICAS=1`, `RASK_SERVE_GPU_FRAC=1.0`, `RASK_IIIF_URL=""`
- `RASK_ORCHESTRATOR_AUTOSTART=false` on the orchestrator; forced `false` everywhere else (single loop).

### 4. GPU, volumes, ordering

- `ray-head`: `gpus: all` + `runtime: nvidia`, `shm_size: '8gb'`, `ulimits.nofile: 65535`, HF-cache volume at `/cache/hf`.
- Named volumes: `pg-data`, `minio-data`, `hf-cache`.
- Startup ordering by container healthchecks + `depends_on: { condition: service_healthy }`:
  - `postgres` (pg_isready) → `migrate` → core-api/orchestrator
  - `minio` (`/minio/health/ready`) → `minio-init` → app services that need buckets
  - `ray-head` healthcheck probes Serve on `:8000/-/healthz` (or app route) so dependents wait for htrflow to be live.
  - No Kubernetes init-containers required — compose healthchecks express the ordering.

### 5. Make targets & demo path

- `make compose-build` — `docker buildx` all 8 images (shared base first), concrete `:dev` tags.
- `make compose-up` — `docker compose up -d --wait`, then print the gateway URL.
- `make compose-down` — `docker compose down` (`--volumes` variant or a `compose-purge` for data wipe).
- `make compose-logs` — tail.
- Coexists with `make dev-micro` / `make ray-up` etc.

**Validated round-trip:** upload images to `minio/images-batch/<vol>/` → `POST /api/v1/batches/<vol>/register` (201, `page_count=N`) → `POST /api/v1/orchestrator/start` (or run the runner directly) → ALTO XML in `images-batch-alto/<vol>/`, viewable through the gateway/frontend.

### 6. Testing / verification

- `docker compose config` parses and validates.
- `make compose-build` succeeds for all images on arm64.
- `make compose-up` brings every long-running service to `healthy`; `migrate` and `minio-init` exit 0.
- End-to-end demo round-trip emits valid ALTO v4 in the output bucket.
- GPU correctness (torch sm_120 in-container) is exercised here — the accepted "assume base" risk. If it fails, document the CPU-only fallback (`RASK_SERVE_GPU_FRAC=0`, drop `gpus: all`).
- `make check` (ruff + ty) stays green for any code touched (e.g. a tiny compose-only config addition, if any).

## Out of scope / follow-ups

- **k3s / helm deploy** — Phase 2, its own spec; this compose work feeds it (shared Dockerfiles).
- KubeRay operator; NATS JetStream orchestrator (stays the in-process singleton).
- search / catalog indexing pipelines in-cluster (one-shot scripts as today; services boot empty).
- Multi-volume-per-chunk in s3 source-mode (one-volume-per-chunk is sufficient).
- Standalone GPU-image spike (folded into first real `compose-up` per the "assume base" decision).
