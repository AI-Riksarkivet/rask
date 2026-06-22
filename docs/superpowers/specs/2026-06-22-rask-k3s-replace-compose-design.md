# rask: replace docker-compose with local k3s — design

Date: 2026-06-22

**Status:** Approved (design), pending implementation plan.

**Relationship to prior work:** Supersedes Phase 2 of
`2026-06-17-rask-local-k3s-volumes-design.md`. Phase 1 of that spec (S3
ingestion, `register_volume`, `RASK_SOURCE_MODE`) is **merged**. The
`2026-06-18-rask-docker-compose-design.md` stack is the working reference
topology this design ports to k3s — and then **removes**.

This design **diverges from the 2026-06-17 spec on two decisions** (re-confirmed
with the user):

1. **KubeRay operator + a `RayService` CRD** instead of a hand-rolled single Ray
   head + a `deploy_serve.py` post-install Job (KubeRay was previously a non-goal).
2. **docker-compose is removed** once k3s is verified (not kept as a fallback).

## Goal

Run the whole `rask` HTR app on one low-resource aarch64/GB10 machine in a
**local single-node k3s** cluster via one `helm upgrade --install`, with a
**single GPU htrflow Serve endpoint**, and retire docker-compose as the local
dev stack.

## What already works (reused as-is)

- **Phase 1 / S3 ingestion is merged.** `register_volume`
  (`components/services/core/src/core/services/registration.py`), the
  `POST /api/v1/batches/{volume_id}/register` endpoint, `RASK_SOURCE_MODE=s3`
  submission branch, and the generic `storage.build_source`/`build_sink` path.
  No application code changes are required by this design.
- **Per-service Dockerfiles exist** under `.docker/`:
  `{gateway,core-api,search-api,volumes-api,ray-api,orchestrator}.dockerfile`,
  `frontend.dockerfile`, and `ray.dockerfile`. The compose builds and runs all
  of them today; **k3s reuses them unchanged.**
- **The GPU image is proven.** `ray.dockerfile` is built on
  `nvidia/cuda:13.0.1-runtime-ubuntu24.04` (arm64 CUDA 13) and the compose
  `ray-head` schedules the GB10 GPU and serves `/htrflow` end-to-end (incl. the
  gated HF TrOCR model via `HF_TOKEN`). The 2026-06-17 spec's "GPU image spike"
  is therefore **resolved** — no spike needed.
- **The htrflow Serve app** is `runner.htrflow_service:htrflow_app` (an
  `Application` from `HTRFlowDeployment.bind()`), route prefix `/htrflow`,
  parametrized by env `RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC`. This becomes
  the `serveConfigV2.import_path` of the RayService.

## Decisions (locked with the user)

- Stateful deps (**Postgres, MinIO, Ray**) all run **in-cluster** — self-contained.
- Packaged by **extending the existing `chart/`** (not a second chart, not raw
  manifests). The chart stays **dual-purpose**: in-cluster deps gated by
  `postgres.enabled` / `minio.enabled` / `ray.enabled`, so disabling them +
  pointing at external Postgres/S3/Ray still serves prod.
- Ray via the **KubeRay operator** and a **`RayService` CRD** (declarative Serve
  app, KubeRay-reconciled).
- Images delivered by **`docker save … | k3s ctr images import -`** (`:dev` tags,
  `pullPolicy: IfNotPresent`). No registry.
- Ingress via the **Traefik bundled with k3s**, one `Ingress` resource.
- **docker-compose is removed** after k3s is verified.
- Work branches off `main`. Commits carry no Claude/AI co-author trailer.

---

## Topology

```
                 Traefik Ingress (host: rask.local)
                   /     → frontend:8080  (SvelteKit SSR, Bun)
                   /api  → gateway:8888
                              │
   ┌───────────┬─────────────┼─────────────┬───────────────┐
 core-api   search-api   volumes-api     ray-api       orchestrator
  :8801       :8802        :8803          :8804        :8810 (singleton)
   │            │            │              │
   └────────────┴────────────┴──────────────┴──── shared core lib
        │                 │                      │
   Postgres           MinIO                  RayService (KubeRay)
  StatefulSet        StatefulSet             GPU head, htrflow Serve @ /htrflow
  :5432 / 8Gi PVC    :9000 / 50Gi PVC        runtimeClassName: nvidia
```

Single node, single GPU. No Ray worker group.

## Components

### Chart rewrite (`chart/`)

Replace the stale `viewer-*` templates (they reference the deleted `viewer`
monolith) with the fleet topology. Keep/adapt `ingress.yaml`, `configmap.yaml`,
`migration-job.yaml`, `serviceaccount.yaml`, `_helpers.tpl`, `NOTES.txt`.

**Fleet (CPU).** One `Deployment` + `Service` per service: `gateway` (:8888,
ingress target), `core-api` (:8801), `search-api` (:8802), `volumes-api`
(:8803), `ray-api` (:8804), `orchestrator` (:8810). Rendered from a
`services.*` values map by a shared template helper. Each:
- `envFrom`: the ConfigMap + the app Secret.
- `/api/health` readiness + liveness probes.
- init-container wait-loops on Postgres / MinIO where required.
- `orchestrator` is **`replicas: 1` + `strategy: Recreate`** (in-process
  singleton; concurrent orchestrators double-submit). It owns
  `RASK_ORCHESTRATOR_AUTOSTART` (default `false`).

**Postgres** (`postgres.enabled`). `StatefulSet` (`postgres:16`, 8Gi PVC,
`pg_isready` probe) + `Service` + a Secret with an auto-generated password,
pinned across upgrades, assembled into `DATABASE_URL`.

**MinIO** (`minio.enabled`). `StatefulSet` (`server /data`, 50Gi PVC, health
probe) + `Service` (:9000) + a Secret with auto-generated creds surfaced as
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` and
`HCP_ENDPOINT=http://rask-minio:9000` / `HCP_INSECURE=true`. A **post-install
buckets Job** (`minio/mc`) waits on health and `mc mb -p` the three buckets
(`images-batch`, `images-batch-alto`, `images-batch-search`).

**Ray** (`ray.enabled`). A **KubeRay `RayService`** CRD:
- `serveConfigV2`: one app, `import_path: runner.htrflow_service:htrflow_app`,
  `route_prefix: /htrflow`, `runtime_env.env_vars` = `HF_TOKEN` (from Secret),
  `HF_HUB_DISABLE_IMPLICIT_TOKEN: "0"`, `RASK_SERVE_REPLICAS: "1"`,
  `RASK_SERVE_GPU_FRAC: "1.0"`.
- `rayClusterConfig.headGroupSpec` pod: the `ray:dev` image,
  `runtimeClassName: nvidia`, `resources.limits.nvidia.com/gpu: 1`, a `/dev/shm`
  memory `emptyDir` (≥30% RAM), an HF-cache PVC at `/cache/hf`,
  `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0`.
- No `workerGroupSpecs` (single node).
- This **replaces `deploy_serve.py` for k3s** — KubeRay reconciles the Serve app
  from the CRD. `deploy_serve.py` / `make serve-*` remain for the non-k3s
  Makefile Ray flow.

**Migration** (`migration-job.yaml`). Post-install/upgrade hook, `core-api:dev`
image, `alembic upgrade head` from the core service dir, `pg_isready`
init-container.

**Ingress** (`ingress.yaml`). Traefik, `className` default empty (k3s Traefik),
host `rask.local`: `/api` → `rask-gateway:8888`, `/` → `rask-frontend:8080`.

**ConfigMap** (`configmap.yaml`). Non-secret env shared by the fleet:
`RASK_SOURCE_MODE=s3`, `RASK_HTR_PIPELINE=htrflow`, `RASK_PREFETCH_PIPELINE=none`,
inter-service base URLs for the gateway, `RAY_DASHBOARD_URL` → the RayService
head service, bucket names, `AWS_REGION`.

### `values.yaml`

Add `postgres`, `minio`, `ray`, `gpu` blocks and a `services.*` map (per-service
image repo, `tag: dev`, `pullPolicy: IfNotPresent`, resources, replicas). Drop
the **required** `existingSecret` gate for local (creds are chart-generated);
keep `existingSecret` as an optional override for prod. Single-node sizing:
RayService head 8Gi/2cpu req · 16Gi lim · `nvidia.com/gpu: 1`; Postgres/MinIO
256Mi req · 1Gi lim; fleet 64–128Mi each. Assumes ≥80Gi free disk.

## Cluster prerequisites — `make k3s-install` (sudo, one-time, idempotent)

In order: install **k3s** (bundled containerd, Traefik, kubectl) → install
**helm** → apply the **NVIDIA k8s device-plugin** DaemonSet and ensure the
`nvidia` containerd runtime + `RuntimeClass` (host already provides
`nvidia-ctk` + `nvidia-smi`) → install the **KubeRay operator**
(`helm repo add kuberay … && helm install kuberay-operator`). Verify
`nvidia.com/gpu: 1` is advertised on the node.

## Make targets

| target | action |
|---|---|
| `make k3s-install` | one-time host setup (k3s + helm + device-plugin + KubeRay operator) |
| `make k3s-build` | `docker buildx build` each `.docker/*.dockerfile` → `:dev` (reuse the `COMPOSE_IMAGES` list + `ray`) |
| `make k3s-import` | `docker save <img>:dev \| sudo k3s ctr images import -` for every fleet + ray + frontend image |
| `make k3s-up` | `helm upgrade --install rask ./chart --wait` → `kubectl rollout status` gateway → print `http://rask.local/` + `/etc/hosts` hint |
| `make k3s-down` | `helm uninstall rask` |
| `make k3s-purge` | `helm uninstall rask` + delete PVCs |

Helm release **pinned to `rask`** so `rask-postgres`, `rask-minio`, and the
RayService head service names resolve in templates.

## Startup ordering

Helm hook-weights order the one-shot Jobs (Postgres ready → migration → MinIO
ready → buckets); init-container wait-loops cover cross-resource readiness;
`/api/health` readiness probes gate fleet traffic. KubeRay reconciles the Serve
app independently; orchestrator `AUTOSTART=false` removes any
Serve-before-submit hard edge.

## Removing docker-compose (after k3s is verified)

Delete `docker-compose.yml`, `.docker/ingress.Caddyfile`,
`.docker/smoke-compose.sh`, and the `compose-*` Make targets. **Keep** every
`.docker/*.dockerfile` (k3s reuses them). Update `README.md`, `CLAUDE.md`, and
`docs/architecture/deployment.md` to document the k3s flow as the local stack.

## Verification

`make k3s-install` → `make k3s-build` → `make k3s-import` → `make k3s-up`:
- all fleet pods Ready; migration + buckets Jobs Complete.
- RayService reports healthy; GPU scheduled on the head (`nvidia-smi` in the head
  pod; `nvidia.com/gpu` requested = 1).
- open `http://rask.local/`; upload images to MinIO `images-batch/<vol>/`;
  `POST /api/v1/batches/<vol>/register`; start the orchestrator; watch ALTO land
  in `images-batch-alto/<vol>/` and render in the viewer.
- `helm lint chart/` and `helm template chart/` render clean.

## Non-goals

- Multi-node / Ray worker groups (single head suffices for one node).
- A local image registry (ctr import is enough).
- In-cluster search/catalog indexing (stays one-shot scripts as today).
- NATS JetStream orchestrator (stays the in-process singleton, pinned to 1).
- Migrating prod off its external-deps model (preserved via `*.enabled` toggles).
