# Deployment

rask ships a **Helm chart at `chart/`** that deploys the application services
(viewer + frontend + an Alembic migration job) to Kubernetes. Postgres, S3/MinIO,
and the KubeRay cluster are **external dependencies** referenced via config and an
operator-created Secret — the chart does not provision them. The `Makefile`
remains the runbook for local/dev operation.

## Container images (`.docker/`)

Three multi-stage, digest-pinned, non-root images. (`.docker/` is excluded from
the build context by `.dockerignore`; images are built from templates.)

| Image | Base | Notes |
|---|---|---|
| `rask-runner` | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | GPU. uv-managed Python + venv; `CMD ["runner"]`. Needs `--shm-size`, `--ulimit nofile=65535`, GPU via nvidia-container-toolkit. |
| `rask-viewer` | `python:3.13-slim-bookworm` | `EXPOSE 8888`; runs `uvicorn viewer.app:app --proxy-headers --forwarded-allow-ips 127.0.0.1`. Set `--forwarded-allow-ips` to the nginx CIDR in prod, never `*`. |
| `rask-frontend` | build on `oven/bun`, serve on `nginxinc/nginx-unprivileged:1.27-alpine` | Builds `component-lib` then the SvelteKit SPA (adapter-static), serves on `:8080` with SPA fallback + immutable asset caching. |

## Ray cluster & Serve (local)

```mermaid
flowchart LR
    rayup["make ray-up<br/><sub>head :6379 · dash :8265</sub>"] --> serveup["make serve-up / serve-up-both<br/><sub>deploy_serve.py</sub>"]
    serveup --> t["/transcribe · TrOCR"]
    serveup --> h["/htrflow · full pipeline"]
```

- `make ray-up` / `ray-down` / `ray-status` — local Ray head.
- `make ray-up-htr` — a **2-GPU pool pinned to GPUs 0,1** (`CUDA_VISIBLE_DEVICES=0,1
  --num-gpus=2`); exports `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and uses
  `uv run --no-sync` (the documented Ray/uv gotcha).
- `make serve-up` / `serve-down` / `serve-status` — deploy the Serve apps via
  `components/scripts/deploy_serve.py`.
- `make serve-up-both` — deploy `transcribe` + `htrflow` with fractional GPU
  reservations (`RASK_SERVE_REPLICAS=2`, `RASK_SERVE_GPU_FRAC=0.49` → ≈1.96 GPU
  on the 2-GPU pool).
- `make qwen-serve` — an external vLLM LLM backend on GPU 2 (isolated venv,
  OpenAI-compatible API on `:8001`), separate from the HTR workspace.

## Remote KubeRay

The runner accepts `--address ray://…:10001`; the viewer's orchestrator submits
jobs to the Ray dashboard REST API at `RAY_DASHBOARD_URL`. **No KubeRay manifests
live in this repo** — the cluster is managed elsewhere (Argo/Helm). The rask Helm
chart (`chart/`) deploys only the app services and points at that cluster via
`config.RAY_DASHBOARD_URL`.

## Helm chart (`chart/`)

`helm install rask chart/ --set existingSecret=<name>` deploys:

- **viewer** — singleton Deployment (`replicas: 1`, `strategy: Recreate`) because
  the in-process orchestrator must not run concurrently. Reaches Ray via
  `RAY_DASHBOARD_URL`, Postgres via `DATABASE_URL`, S3 via `AWS_*`/`HCP_*`.
- **frontend** — scalable Deployment serving the SPA on `:8080`.
- **migration** — pre-install/pre-upgrade hook Job running `alembic upgrade head`.
- **Ingress** — `/api` → viewer:8888, `/` → frontend:8080.

Sensitive config comes from an operator-created Secret (`existingSecret`);
non-sensitive config from `values.yaml` → ConfigMap. See `chart/README.md`.

## CI

- **GitHub Actions** runs only `.github/workflows/docs.yml`: builds this Zensical
  site (`zensical build --clean`, with mkdocstrings API reference), builds
  Storybook, and deploys both to GitHub Pages on push to `main`/`master`.
- **Tests & migrations** run through **Dagger**, not GitHub Actions:
  `dagger call migrate-up` (alembic against an ephemeral Postgres — proof of a
  clean from-zero migration) and `dagger call test-pg` (migrate + viewer pytest).

## State stores

- **Postgres** (prod) via `DATABASE_URL=postgresql+asyncpg://…`; **SQLite** (dev)
  at `.cache/batches.db`. Schema changes go through **Alembic** — never
  `create_all` at startup. Local Postgres: `make pg-up` / `pg-migrate`.
- **S3 / HCP** two-bucket setup (`images-batch` input, `images-batch-alto`
  output) plus the `images-batch-search` Lance tables.
