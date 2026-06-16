# Deployment

rask uses a **Helm chart at `chart/`** for Kubernetes deployment, plus a
`Makefile` for local/dev operation. Postgres, S3/MinIO, and the KubeRay cluster
are **external dependencies** referenced via config and an operator-created
Secret — the chart does not provision them.

!!! warning "Helm chart and viewer dockerfile are stale (deployment-cycle follow-up)"
    The Helm chart (`chart/`) and `.docker/viewer.dockerfile` still reference the
    old monolithic `viewer` service, which was dissolved in June 2026. They are a
    known follow-up task — not yet updated to the gateway + per-domain services
    fleet. Do not rely on the chart as-is for the current service topology.
    `make dev-micro` and `dev-micro.sh` are the authoritative source for the
    current fleet.

## Container images (`.docker/`)

Production-shaped image definitions live at `.docker/`. Three are current:

| Image | Base | Notes |
|---|---|---|
| `rask-runner` | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | GPU. uv-managed Python + venv; `CMD ["runner"]`. Needs `--shm-size`, `--ulimit nofile=65535`, GPU via nvidia-container-toolkit. |
| `rask-frontend` | build on `oven/bun`, serve on `nginxinc/nginx-unprivileged:1.27-alpine` | Builds `component-lib` then the SvelteKit SPA (adapter-static), serves on `:8080` with SPA fallback + immutable asset caching. |

`.docker/viewer.dockerfile` references the dissolved monolith and is pending update
to the new per-service entrypoints (gateway, core-api, orchestrator, volumes-api,
search-api, ray-api).

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

The runner accepts `--address ray://…:10001`; the orchestrator submits jobs to
the Ray dashboard REST API at `RAY_DASHBOARD_URL`. **No KubeRay manifests live
in this repo** — the cluster is managed elsewhere (Argo/Helm). The rask Helm
chart (`chart/`) deploys only the app services and points at that cluster via
`config.RAY_DASHBOARD_URL`.

## Helm chart (`chart/`) — stale, pending update

!!! warning
    The chart currently targets the old `viewer` monolith and is a known
    deployment-cycle follow-up. The description below reflects the chart's
    *current* state, not the target topology.

`helm install rask chart/ --set existingSecret=<name>` currently deploys:

- **viewer** — singleton Deployment (`replicas: 1`, `strategy: Recreate`) — this
  is the old monolith, pending replacement by gateway + per-domain service Deployments.
- **frontend** — scalable Deployment serving the SPA on `:8080`.
- **migration** — pre-install/pre-upgrade hook Job running `alembic upgrade head`.
- **Ingress** — `/api` → viewer:8888, `/` → frontend:8080.

The target topology is: gateway + core-api + orchestrator + volumes-api +
search-api + ray-api Deployments, with the Ingress pointing only at the gateway
(`/api` → gateway:8888) and the `replicas: 1` / `Recreate` constraint moving to
only the `orchestrator` Deployment.

Sensitive config comes from an operator-created Secret (`existingSecret`);
non-sensitive config from `values.yaml` → ConfigMap. See `chart/README.md`.

## CI

- **GitHub Actions** runs only `.github/workflows/docs.yml`: builds this Zensical
  site (`zensical build --clean`, with mkdocstrings API reference), builds
  Storybook, and deploys both to GitHub Pages on push to `main`/`master`.
- **Tests & migrations** run through **Dagger**, not GitHub Actions:
  `dagger call migrate-up` (alembic against an ephemeral Postgres — proof of a
  clean from-zero migration) and `dagger call test-pg` (migrate + core pytest).

## State stores

- **Postgres** (prod) via `DATABASE_URL=postgresql+asyncpg://…`; **SQLite** (dev)
  at `.cache/batches.db`. Schema changes go through **Alembic** — never
  `create_all` at startup. Local Postgres: `make pg-up` / `pg-migrate`.
- **S3 / HCP** two-bucket setup (`images-batch` input, `images-batch-alto`
  output) plus the `images-batch-search` Lance tables.
