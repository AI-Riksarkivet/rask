# Migrate Helm chart from MinIO/single-Postgres to RustFS + CloudNativePG operators

Date: 2026-06-25
Status: Approved (design) — pending implementation plan
Branch: `feat/rustfs-cnpg-operators`

## Goal

Replace the two hand-rolled in-cluster backends in the `rask` Helm chart with
operator-managed equivalents:

- **MinIO StatefulSet → RustFS operator** (`Tenant` CR, S3-compatible object store).
- **Single Postgres StatefulSet → CloudNativePG (CNPG) operator** (`Cluster` CR,
  lifecycle-managed Postgres).

The chart must keep its current properties:

- **One `make k3s-up` brings up everything** (operators + CRs + app), matching the
  existing KubeRay precedent.
- **`*.enabled` gating preserved** so a production install pointing at externally
  managed Postgres/S3 (via `existingSecret`) can turn the in-cluster backends off.

## Scope & non-goals

- **In scope**: chart changes (deps, templates, values, helpers, NOTES), Makefile
  dependency vendoring, docs, local greenfield cutover.
- **Greenfield**: old `rask-minio` / `rask-postgres` PVCs are purged
  (`make k3s-purge`); **no data migration**. Single-node local k3s.
- **Out of scope**: CNPG backups/WAL archiving, RustFS distributed/erasure-coded
  mode, multi-node HA tuning, OpenFGA datastore adoption, prod external-infra
  rollout (the `*.enabled=false` path is preserved but not exercised here).

## Background: how operators differ from the current StatefulSets

Both new backends are **operators** — they install CRDs + a controller, and you
declare a custom resource the controller reconciles into Services + StatefulSets.

The existing chart already has a precedent: **KubeRay** is a gated subchart
dependency (`kuberay.enabled`) and the `RayService` CR is a plain gated template
(`templates/rayservice.yaml`). That works because (1) Helm installs all subcharts'
`crds/` before any template, and (2) KubeRay has **no blocking admission webhook**,
so the CR can be created before the operator is Ready and is reconciled later.

Two operator-specific differences drive this design:

- **RustFS** — controller-only reconciliation, **no admission webhook** (operator
  ns `rustfs-system`, `Tenant` in `rustfs.com/v1alpha1`). Behaves exactly like
  KubeRay → plain gated CR template, no hook needed.
- **CNPG** — installs a validating/mutating **webhook** defaulting to
  `failurePolicy: Fail`. On a fresh install, a `Cluster` applied before the
  operator webhook pod is serving would be **rejected** and `helm install`
  errors. The webhook `failurePolicy` is configurable via the operator chart
  values, so we set **`failurePolicy: Ignore`** for the validating *and*
  mutating webhooks. A `Cluster` created before the operator is Ready is then
  simply admitted and reconciled once the controller comes up — exactly the
  KubeRay/`RayService` behavior — so the `Cluster` is a plain gated template,
  **no Helm hook**. (A post-install hook on a *stateful* `Cluster` was rejected:
  Helm hook delete-policies would either drop the database on every upgrade
  (`before-hook-creation`) or error on re-create (no policy). The webhook only
  performs synchronous defaulting/validation; the operator re-validates at
  reconcile time and the `Cluster` spec is chart-templated, so the brief
  Ignore window during install/upgrade is low-risk.)

A second asymmetry is **packaging**:

- **CNPG** publishes a Helm repo (`https://cloudnative-pg.github.io/charts`,
  chart `cloudnative-pg`) → normal remote subchart dependency, vendored by the
  existing `helm dependency build`.
- **RustFS operator** ships a Helm chart **in-repo** (`deploy/rustfs-operator/`,
  with packaged CRDs) but has **no published Helm repository** → it is **vendored
  locally** into `chart/charts/rustfs-operator/` (committed, unpacked subchart,
  pinned to an upstream git tag), gated by `rustfs.enabled`.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Install model | Gated subchart operators + CR templates, matching the KubeRay style |
| CNPG `Cluster` ordering | Webhook `failurePolicy: Ignore` + plain gated template (no hook) |
| CNPG topology | **1 instance**, operator-managed (no replica) |
| RustFS mode | **Standalone** = `servers:1, volumesPerServer:4` → 1 pod / 4 PVCs (erasure-coding minimum) |
| RustFS buckets | Declared natively on the Tenant (`spec.buckets`) — no `mc` Job |
| RustFS operator packaging | **Vendored** at `third_party/rustfs-operator/`, `file://` subchart dep |
| CNPG operator packaging | Remote subchart dependency |
| Data | **Greenfield** — purge old PVCs, redeploy, no migration |
| Working copy | `/home/morgan/rask`, branch `feat/rustfs-cnpg-operators` |

## Design

### 1. Chart dependencies (`chart/Chart.yaml`)

- Add **CNPG** as a remote subchart dependency:
  - repository `https://cloudnative-pg.github.io/charts`, chart `cloudnative-pg`,
    version **`0.28.3`** (appVersion 1.29.1), `condition: cnpg.enabled`.
  - Operator subchart value overrides (under key `cloudnative-pg:`) set both
    webhook failure policies to `Ignore`:
    `webhook.validating.failurePolicy` / `webhook.mutating.failurePolicy`.
- Add **RustFS operator** as a **vendored local** subchart. `chart/charts/` is
  gitignored (rebuilt by `helm dependency build`), so the committed source lives
  at repo-root **`third_party/rustfs-operator/`** and is referenced as a
  dependency `repository: "file://../third_party/rustfs-operator"`, name
  `rustfs-operator`, version `0.1.0`, `condition: rustfs.enabled`.
  `helm dependency build` packages it into `chart/charts/`; its packaged CRDs
  install first. Operator image `rustfs/operator:latest` is published upstream
  (no source build).
- `chart/Chart.lock` regenerated (`helm dependency update`) for both additions.

### 2. CloudNativePG resources

- **`chart/templates/cnpg-cluster.yaml`** — a `Cluster` named **`rask-postgres`**
  (`apiVersion: postgresql.cnpg.io/v1`):
  - `instances: 1`; `storage.size` + `storage.storageClass: local-path` from values;
    `imageName: ghcr.io/cloudnative-pg/postgresql:16`.
  - `bootstrap.initdb`: database `rask`, owner `rask`, `secret.name` referencing a
    basic-auth Secret we control (so the password is the pinned
    `secrets.postgresPassword` from `.env`, never an operator-random value).
  - **Plain `cnpg.enabled`-gated template, no Helm hook** (webhook `failurePolicy:
    Ignore` makes cold-install admission safe). `make k3s-up --wait` waits for the
    cluster + the migrate Job's `nc` init handles "DB not ready yet".
  - Primary read/write service: **`rask-postgres-rw:5432`**.
- **`chart/templates/secrets.yaml`** — replace the `rask-postgres` `POSTGRES_*`
  Secret with a `kubernetes.io/basic-auth` Secret (`username: rask`,
  `password: <pgPassword>`) referenced by the `Cluster`'s `initdb`.

### 3. RustFS resources

- **`chart/templates/rustfs-tenant.yaml`** — a `Tenant` named **`rask-rustfs`**
  (`apiVersion: rustfs.com/v1alpha1`):
  - One pool, `servers: 1`, `persistence.volumesPerServer: 4` → **1 pod / 4 PVCs**
    (RustFS erasure-coding minimum is `servers * volumesPerServer >= 4`; there is
    no true single-PVC mode). PVC template `storage` + `storageClassName` from values.
  - `credsSecret.name` → a Secret (`rask-rustfs`, replacing `rask-minio`) with keys
    **`accesskey`/`secretkey`** (operator requires these names, min 8 chars) =
    `secrets.minioAccessKey` / `secrets.minioSecretKey`.
  - **`spec.buckets`** declares the three buckets natively
    (`images-batch`, `images-batch-alto`, `images-batch-search`) — the operator
    provisions them, so **no `mc`/`aws` Job**.
  - Plain `rustfs.enabled`-gated template (no hook — no blocking webhook).
  - S3 service: **`rask-rustfs-io:9000`** (confirmed from operator source
    `io_service_name = {tenant}-io`), console `rask-rustfs-console:9001`.

### 4. `chart/values.yaml` restructure

- `postgres:` block → **`cnpg:`** (`enabled`, `instances: 1`, `storage`,
  `storageClass`, pg image/version, `database`, `user`, `resources`).
- `minio:` block → **`rustfs:`** (`enabled`, `mode: standalone`, `storage`,
  `storageClass`, `buckets`, ports, operator/tenant image refs, `resources`).
- The `cnpg:` / `rustfs:` keys double as both the operator-subchart value override
  scope and the `*.enabled` toggles (same convention as `kuberay`/`nats`/`dapr`).
- **Unchanged** (avoid churn in `Makefile` k3s-up `--set-string` and `.env`):
  `secrets.postgresPassword`, `secrets.minioAccessKey`, `secrets.minioSecretKey` —
  they now feed the new Secrets.

### 5. Connection wiring

- **`chart/templates/_helpers.tpl`**
  - `rask.databaseUrl`: host `rask-postgres` → **`rask-postgres-rw`** (rest of the
    `postgresql+asyncpg://` URL unchanged; password still `rask.pgPassword`,
    still pinned via `lookup`).
  - `rask.pgPassword` lookup target updated to the new basic-auth Secret/key.
  - `rask.minioSecretKey` lookup target updated to the new `rask-rustfs` Secret/key.
- **`chart/templates/secrets.yaml`** (`rask-app`): `HCP_ENDPOINT` →
  **`http://rask-rustfs-io:9000`**.
- **`chart/templates/fleet.yaml`**: `services.*.waitFor` keys stay
  `"postgres"`/`"minio"` (logical deps); only the `nc` target hosts change to
  `rask-postgres-rw` / `rask-rustfs-io`.
- **`chart/templates/migration-job.yaml`**: `wait-postgres` initContainer `nc`
  target → `rask-postgres-rw`.

### 6. Files

- **Removed**: `chart/templates/minio.yaml` (incl. the `mc` bucket Job),
  `chart/templates/postgres.yaml`.
- **Added**: `chart/templates/cnpg-cluster.yaml`,
  `chart/templates/rustfs-tenant.yaml`, `third_party/rustfs-operator/` (vendored
  operator chart), `scripts/vendor-rustfs-operator.sh`.
- **Changed**: `chart/Chart.yaml`, `chart/Chart.lock`, `chart/values.yaml`,
  `chart/templates/secrets.yaml`, `chart/templates/_helpers.tpl`,
  `chart/templates/fleet.yaml`, `chart/templates/migration-job.yaml`,
  `chart/templates/NOTES.txt`, `Makefile` (`K3S_DEP_REPOS` += cnpg; `k3s-deps`
  vendoring note), `chart/README.md`, `CLAUDE.md` (deployment paragraph),
  `docs/architecture/deployment.md`.

### 7. Dev workflow / Makefile

- `k3s-deps`: add the cnpg Helm repo to `K3S_DEP_REPOS`; ensure the vendored
  `chart/charts/rustfs-operator/` is present (a `scripts/vendor-rustfs-operator.sh`
  documents/refreshes the pinned upstream tag).
- Greenfield cutover documented: `make k3s-purge` (drops old `rask-minio` /
  `rask-postgres` PVCs) → `make k3s-up`.

## Verification

- `helm lint` + `helm template` render clean with `cnpg.enabled`/`rustfs.enabled`
  **on** and **off** (the prod external-infra path).
- Fresh `make k3s-purge && make k3s-up` on the node:
  - CNPG + RustFS operators reconcile; `Cluster` and `Tenant` reach Ready.
  - Buckets auto-provisioned via the Tenant's `spec.buckets`.
  - Migrate Job succeeds (Alembic `upgrade head` against `rask-postgres-rw`).
  - `rask-gateway` rolls out; `/api/health` green.
  - S3 round-trip via `scripts/smoke_s3.py` against `rask-rustfs-io:9000`; a DB
    read confirms connectivity.

## Live verification results (2026-06-25, greenfield k3s)

Verified end-to-end on the single-GPU k3s node (install with `ray.enabled=false`,
`dapr.sidecars=false`, and `--set-string secrets.postgresPassword=… secrets.minioSecretKey=…`):
CNPG `Cluster` healthy + Alembic migrations applied (`alembic_version` head +
`batches`), RustFS `Tenant` Running with **all three buckets auto-provisioned via
`spec.buckets`**, S3 put→get round-trip OK, gateway `/api/health` → 200.

Two chart fixes were required and are committed (verified live):

- **CNPG CRD cold-install race** — CNPG ships its CRDs as *templated* resources
  (`templates/crds/crds.yaml`), not a bare `crds/` dir, so Helm cannot map the
  `Cluster` CR on a cold install (`no matches for kind "Cluster"`). The webhook
  `failurePolicy: Ignore` does **not** help this (it's a CRD-establishment, not
  webhook, race). Fix: vendor the CNPG CRDs into the umbrella chart's
  **`chart/crds/cnpg-crds.yaml`** (installed in Helm's CRD phase) and set
  `cloudnative-pg.crds.create: false` so the subchart doesn't double-apply them.
  (RustFS's own CRDs ship in a bare `crds/` dir, so it never hit this.)
- **RustFS single-node disk check** — RustFS erasure coding refuses 4 `local-path`
  volumes on one physical disk. Fix: values-gated `rustfs.bypassDiskCheck: true`
  → `RUSTFS_UNSAFE_BYPASS_DISK_CHECK` env on the Tenant (local/CI only).

Two **pre-existing** issues surfaced (not caused by this migration, not fixed here):

- **dapr injector race on fresh install** — fleet pods created before the dapr
  injector webhook is serving come up sidecar-less (`failurePolicy: Ignore`) →
  gateway `/api/health` 502. Workaround: rollout-restart the fleet once the
  injector is Ready, or install with `dapr.sidecars=false`.
- **Random-password requirement** — with `secrets.postgresPassword` /
  `secrets.minioSecretKey` unset and no prior secret to `lookup`, the
  `randAlphaNum` fallback differs between the credential Secret and the
  `DATABASE_URL` / app AWS creds → auth fails. Same contract as the old chart
  (provide them via `.env` or `--set`). The verified install set them explicitly.

## Risks / to verify at implementation

- **RustFS bucket provisioning** (operator v0.x feature): confirm `spec.buckets`
  actually creates the buckets at reconcile against the running tenant. Fallback
  if flaky: a post-install `aws s3api`/`mc` Job against `rask-rustfs-io:9000`
  (documented, not implemented by default).
- **RustFS operator maturity**: latest git tag is `0.0.2`, chart `0.1.0`. Pin a
  specific ref when vendoring; verify the `Tenant` CRD schema in the vendored
  chart matches the templated fields.
- **CNPG webhook `failurePolicy: Ignore`** is the mechanism that makes single
  `helm install` cold-start reliable; confirm the operator installs cleanly into
  the `default` release namespace (KubeRay already does as a subchart).
- **`file://` subchart dep**: confirm `helm dependency update/build` resolves
  `file://../third_party/rustfs-operator` and packages it into `chart/charts/`
  without circular-path issues.
- **Vendored chart upkeep**: refresh `third_party/rustfs-operator/` against
  upstream tags via `scripts/vendor-rustfs-operator.sh`; CRD upgrades are not
  auto-managed by Helm (`charts/*/crds/` are install-only).
