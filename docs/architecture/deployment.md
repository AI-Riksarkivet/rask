# Deployment

rask uses a **Helm chart at `chart/`** as the single deploy artifact for both
local k3s and production Kubernetes. The chart supports in-cluster
CloudNativePG (Postgres), RustFS (object store), and KubeRay via
`cnpg.enabled` / `rustfs.enabled` / `ray.enabled` toggles — each toggle gates
both the operator subchart and the custom resource it manages. Set all three to
`true` for local k3s; leave them `false` for production (external deps supplied
via `existingSecret`).

## Local deploy (k3s)

```bash
make k3s-install      # one-time: k3s + helm + NVIDIA device-plugin + KubeRay (sudo)
make k3s-build        # build fleet + frontend + ray images as :dev
make k3s-import       # side-load images into k3s
make k3s-up           # helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make e2e              # verify MFE hydration + API round-trip end-to-end (run after k3s-up)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```

`k3s-build` builds the full fleet (gateway, core-api, search-api, volumes-api,
ray-api, orchestrator) plus the frontend and ray images as `:dev` tags.
`k3s-import` side-loads them into k3s containerd via `docker save | ctr images
import`, so no registry is needed.

## Container images (`.docker/`)

Production-shaped image definitions live at `.docker/`, built with `docker buildx`
(repo root as context). Current:

| Image | Base | Notes |
|---|---|---|
| `rask-runner` | `nvidia/cuda:12.4.0-runtime-ubuntu22.04` | GPU. uv-managed Python + venv; `CMD ["runner"]`. Needs `--shm-size`, `--ulimit nofile=65535`, GPU via nvidia-container-toolkit. |
| `rask-frontend` | build + serve on `oven/bun:1-debian` | The catch-all SvelteKit app, **SSR via `svelte-adapter-bun`** (no longer an nginx SPA). Pre-builds `@rask/ui`, then `bun build`; the final stage ships the Bun runtime + `node_modules` and runs `bun build/index.js` on `:3000`. tini as PID 1, non-root UID 10001. |
| `rask-storage-frontend` | build + serve on `oven/bun:1-debian` | The Storage microfrontend (`base /storage`). Same Bun-SSR shape as `rask-frontend`; healthcheck hits `/storage`. |
| `rask-compute-frontend` | build + serve on `oven/bun:1-debian` | The Compute/Ray microfrontend (`base /compute`). Same Bun-SSR shape; healthcheck hits `/compute/overview`. |

The three frontend images share one non-obvious build contract (documented in each
dockerfile header): `svelte-adapter-bun` externalizes `@sveltejs/kit`, so the final
image must ship `node_modules` — `build/` is not standalone. And bun 1.3's **isolated
linker** keeps real packages in `node_modules/.bun/` with per-member symlinks, so the
final stage copies both the root store **and** the app dir (with its symlinks) and
runs from the app dir. Each image `COPY`s the full JS workspace (`frontend`,
`storage-frontend`, `compute-frontend`, `packages/{api,ui}`) so `bun install` resolves
— siblings are build-stage only, never shipped.

!!! note "Frontend images aren't built by `.dagger`"
    The Dagger module covers Python CI only (`migrate-up`, `test-pg` — see
    [CI](#ci) below); it does **not** build or publish the `.docker/*.dockerfile`
    images. Those are built directly with `docker buildx`. Wiring the frontend (and
    service) image builds into Dagger or a GitHub Actions matrix is a
    deployment-cycle follow-up.

`.docker/viewer.dockerfile` references the dissolved monolith and is pending update
to the new per-service entrypoints (gateway, core-api, orchestrator, volumes-api,
search-api, ray-api).

The frontend topology those images serve — the Turborepo vertical-microfrontend
proxy, the shared `@rask/ui` shell, and per-app `kit.paths.base` — is documented in
[Frontend microfrontends](frontend-microfrontends.md).

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
the Ray dashboard REST API at `RAY_DASHBOARD_URL`. With `ray.enabled=true` the
chart provisions an in-cluster KubeRay RayService; with `ray.enabled=false` the
orchestrator points at an external cluster via `config.RAY_DASHBOARD_URL`.

## Helm chart (`chart/`)

`make k3s-up` runs `helm upgrade --install rask ./chart --wait`. The chart deploys:

- **gateway** — reverse proxy on `:8888` (path-routes `/api/*` to per-domain services).
- **core-api** — batches + chunks + catalog endpoints on `:8801`.
- **orchestrator** — the reconcile loop on `:8810` (`replicas: 1`, `Recreate`).
- **volumes-api** — S3/IIIF image + ALTO proxy on `:8803`.
- **search-api** — Lance FTS + S3 thumbnails on `:8802`.
- **ray-api** — Ray dashboard proxy + `/api/serve/*` on `:8804`.
- **frontend** — SvelteKit SSR app on `:3000`.
- **migration** — pre-install/pre-upgrade hook Job running `alembic upgrade head`.
- **Ingress** (Traefik) — `/api` → gateway:8888, `/` → frontend:3000.
- **In-cluster deps** (gated by values toggles — each gate covers both the operator subchart and its CR):
  - `cnpg.enabled` — CloudNativePG operator + `Cluster` named `rask-postgres`; app connects to `rask-postgres-rw:5432`. Values under `cnpg.*` (instances, storage, imageName, user, database).
  - `rustfs.enabled` — RustFS operator (vendored at `third_party/rustfs-operator/`, refreshed via `scripts/vendor-rustfs-operator.sh`) + `Tenant` named `rask-rustfs`; S3 at `rask-rustfs-io:9000`, console at `rask-rustfs-console:9001`. Standalone mode: 1 pod / 4 PVCs (erasure-coding minimum). Buckets provisioned natively via `spec.buckets` — no init Job. Values under `rustfs.*`.
  - `ray.enabled` — KubeRay `RayService` (head + worker with GPU limits).

Greenfield local cutover (drops old PVCs): `make k3s-purge && make k3s-up`.

Sensitive config comes from an operator-created Secret (`existingSecret`, default
`rask-app`); non-sensitive config from `values.yaml` → ConfigMap. See
`chart/README.md`.

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
