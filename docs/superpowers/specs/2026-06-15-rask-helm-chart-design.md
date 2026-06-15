# rask Helm chart — design

Date: 2026-06-15

## Goal

Add a Helm chart at `chart/` so the rask **application services** can be
deployed to a Kubernetes cluster with `helm install`. The chart is
deploy-tool-agnostic (plain Helm) and trivially wrappable in an Argo CD
`Application` later to fit the existing dev-kuberay GitOps setup.

This reverses the prior "no Helm" stance documented in
`docs/architecture/deployment.md`; that doc must be updated as part of the work.

## Scope

**In scope** — the chart deploys:

- **viewer** — `rask-viewer` image, FastAPI on `:8888`. The only HTTP backend;
  hosts the in-process orchestrator loop.
- **frontend** — `rask-frontend` image, nginx-unprivileged serving the SvelteKit
  SPA on `:8080`.
- **Alembic migration** — a pre-install/pre-upgrade `Job` running
  `alembic upgrade head` from the viewer image.
- **Config + networking** — ConfigMap (non-sensitive env), Ingress, Service
  per component, ServiceAccount.

**Out of scope** (referenced as external, never deployed by this chart):

- **Postgres** — supplied via `DATABASE_URL` in an existing Secret.
- **S3 / MinIO / HCP** — supplied via `AWS_*` / `HCP_*` in an existing Secret.
- **KubeRay cluster + Ray Serve apps** (`/transcribe`, `/htrflow`) — managed
  elsewhere; reached via a `ray://…:10001` address in the ConfigMap.
- **runner** — the short-lived GPU job-submitter is **not** deployed. The
  viewer's orchestrator submits work to the existing Ray cluster
  (`htr_http` pipeline). The runner stays a CLI / externally-driven RayJob.

## Critical constraint: viewer is a singleton

The orchestrator runs as a lifespan-managed `asyncio.Task` **inside the viewer**
(`viewer/services/orchestrator_loop.py`). Running more than one viewer replica
would start N concurrent orchestrators, all reconciling S3 and submitting jobs →
**double submission**.

Therefore:

- `viewer.replicas` defaults to `1` and the chart documents that scaling the
  viewer is unsafe without disabling the orchestrator on extra replicas or
  adding leader election (both out of scope).
- viewer Deployment uses `strategy: Recreate` (not RollingUpdate) so two
  orchestrators never overlap during a rollout.
- frontend has no such constraint and may scale freely.

## Chart layout

```
chart/
  Chart.yaml            # name: rask; appVersion tracks the image tags
  values.yaml           # documented defaults
  templates/
    _helpers.tpl        # name/label/selector helpers
    serviceaccount.yaml
    configmap.yaml          # non-sensitive env (see Config)
    viewer-deployment.yaml  # replicas:1, Recreate, envFrom secret+configmap
    viewer-service.yaml     # ClusterIP :8888
    frontend-deployment.yaml
    frontend-service.yaml   # ClusterIP :8080
    migration-job.yaml      # helm hook pre-install,pre-upgrade
    ingress.yaml
    NOTES.txt
```

## Configuration

### Sensitive — existing Secret (operator-created, out-of-band)

`viewer.existingSecret` names a Secret consumed by both the viewer Deployment and
the migration Job via `envFrom.secretRef`. Expected keys:

- `DATABASE_URL` — `postgresql+asyncpg://…`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL` (and/or `HCP_*`)
- `HF_TOKEN` — Riksarkivet HF org token for private models

Nothing secret is ever rendered into `values.yaml` or a chart-managed Secret.

### Non-sensitive — ConfigMap (`envFrom.configMapRef`)

Driven from `values.yaml`:

- `RASK_RAY_ADDRESS` — e.g. `ray://rask-head:10001`
- `RASK_ORCHESTRATOR_INTERVAL_SECONDS`
- `RASK_ORCHESTRATOR_AUTOSTART` — **default `false`**. Installs are inert until an
  operator flips it at runtime via `POST /api/v1/orchestrator/start`.
- S3 bucket names (input `images-batch`, output `images-batch-alto`, search
  `images-batch-search`)
- `IIIF_*` source settings
- viewer `--forwarded-allow-ips` CIDR (the ingress controller's pod CIDR — never `*`)

## Migrations

`migration-job.yaml` runs `alembic upgrade head` using the viewer image, with:

```yaml
annotations:
  "helm.sh/hook": pre-install,pre-upgrade
  "helm.sh/hook-weight": "-5"
  "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
```

It pulls `DATABASE_URL` from the same existing Secret. This enforces the
"Alembic, never `create_all`" rule and guarantees the schema is current before
the viewer rolls. Toggleable via `migrations.enabled` (default `true`).

## Networking

A single `Ingress` (all fields values-driven, sane defaults):

| Path | Backend |
|---|---|
| `/api` (incl. `/api/ray`) | `viewer` Service `:8888` |
| `/` | `frontend` Service `:8080` |

The SPA calls `/api/*` relative; the frontend nginx does **not** proxy `/api`
(confirmed in `.docker/frontend.nginx.conf`), so the Ingress owns that split.

Values: `ingress.enabled` (default `true`), `ingress.className` (default
`nginx`), `ingress.host`, `ingress.tls` (default off), `ingress.annotations`.

## Images

Per-component `image.repository` / `image.tag` / `image.pullPolicy`, plus
chart-level `imagePullSecrets`.

**Prerequisite, not solved here:** there is currently no CI pushing these images
— `.github/workflows/docs.yml` only builds the docs site. The chart assumes
`rask-viewer` and `rask-frontend` already exist at a configured registry.
Building/pushing them (CI or manual) is a separate task; the chart will document
the expected image contract but not produce the images.

## Standard knobs (values-driven, conventional defaults)

`resources` (requests/limits per component), `nodeSelector`, `tolerations`,
`affinity`, `podSecurityContext` / `securityContext` (non-root, matching the
images), `livenessProbe` / `readinessProbe` (viewer: HTTP on `:8888`; frontend:
HTTP on `:8080`), `frontend.replicas` (default 2).

## Testing / validation

- `helm lint chart/`
- `helm template chart/ -f <test-values>` renders cleanly for: defaults,
  ingress disabled, migrations disabled, TLS enabled.
- Manual `helm install --dry-run` against the target cluster once a real
  existing Secret + image registry are available.

## Docs to update

- `docs/architecture/deployment.md` — replace the "no Helm chart" statement and
  the "`chart/` is an empty placeholder" note with a section describing this
  chart, its scope, and the singleton-viewer constraint.
- `CLAUDE.md` — the "No … Helm" line in the Architecture section.

## Out of scope (explicit)

- Postgres / MinIO / KubeRay provisioning (external dependencies).
- Multi-replica viewer / orchestrator leader election.
- Image build & push pipeline.
- Argo CD `Application` manifest (chart is Argo-ready; wrapping is a later step).
