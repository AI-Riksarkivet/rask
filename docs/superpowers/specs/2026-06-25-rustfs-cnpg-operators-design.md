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
- **CNPG** — installs a validating/mutating **webhook** with `failurePolicy: Fail`.
  On a fresh install, a `Cluster` applied before the operator webhook pod is
  serving is **rejected** and `helm install` errors. CRD-first ordering is not
  enough; the webhook pod must be Ready. → the `Cluster` CR is rendered as a
  **`post-install,post-upgrade` hook** so Helm (`--wait`, used by `make k3s-up`)
  brings the operator to Ready before the hook applies the `Cluster`.

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
| CNPG `Cluster` ordering | Rendered as a `post-install,post-upgrade` hook (beats the webhook race) |
| CNPG topology | **1 instance**, operator-managed (no replica) |
| RustFS mode | **Standalone** (1 pod / 1 PVC) |
| RustFS operator packaging | **Vendored locally** under `chart/charts/rustfs-operator/` |
| CNPG operator packaging | Remote subchart dependency |
| Data | **Greenfield** — purge old PVCs, redeploy, no migration |
| Working copy | `/home/morgan/rask`, branch `feat/rustfs-cnpg-operators` |

## Design

### 1. Chart dependencies (`chart/Chart.yaml`)

- Add **CNPG** as a remote subchart dependency:
  - repository `https://cloudnative-pg.github.io/charts`, chart `cloudnative-pg`,
    pinned version (~`0.28.x`, confirm latest at implementation), `condition: cnpg.enabled`.
- Add **RustFS operator** as a **local** subchart: `chart/charts/rustfs-operator/`
  (vendored, unpacked), `condition: rustfs.enabled`. Because it is unpacked under
  `charts/`, Helm treats it as a normal subchart; its packaged CRDs install first.
  - It is **not** added under `dependencies:` with a repository (there is no repo
    to fetch from). A vendored local chart in `charts/` is picked up directly.
- `chart/Chart.lock` regenerated for the CNPG addition.

### 2. CloudNativePG resources

- **`chart/templates/cnpg-cluster.yaml`** — a `Cluster` named **`rask-postgres`**:
  - `instances: 1`; `storage.size` + `storage.storageClass: local-path` from values.
  - `bootstrap.initdb`: database `rask`, owner `rask`, password sourced from a
    basic-auth Secret we control (so the password is the pinned
    `secrets.postgresPassword` from `.env`, never an operator-random value).
  - Rendered as a **`post-install,post-upgrade` hook**, `helm.sh/hook-weight`
    **lower than** the migrate Job (so the cluster is admitted before migrations run).
  - Primary read/write service: **`rask-postgres-rw:5432`**.
- **`chart/templates/secrets.yaml`** — replace the `rask-postgres` `POSTGRES_*`
  Secret with a `kubernetes.io/basic-auth` Secret (`username: rask`,
  `password: <pgPassword>`) referenced by the `Cluster`'s `initdb`.

### 3. RustFS resources

- **`chart/templates/rustfs-tenant.yaml`** — a `Tenant` named **`rask-rustfs`**:
  - Standalone (1 pod / 1 PVC); `storage` + `storageClass` from values.
  - Credentials from a Secret (`rask-rustfs`, replacing `rask-minio`) holding the
    root access/secret key = `secrets.minioAccessKey` / `secrets.minioSecretKey`.
    (Exact Secret key names + `Tenant` spec fields pinned against the vendored
    chart's `values.yaml`/examples at implementation.)
  - Plain `rustfs.enabled`-gated template (no hook — no blocking webhook).
  - S3 service: **`rask-rustfs-io:9000`**, console `rask-rustfs-console:9001`.
- **Bucket provisioning** — keep the existing post-install Job pattern (today's
  `mc` job), repointed at `rask-rustfs-io:9000`. RustFS is S3-compatible, so
  `mc` / `aws s3api` create the three buckets (`images-batch`,
  `images-batch-alto`, `images-batch-search`). Weighted after the Tenant is up.
  (May live in `rustfs-tenant.yaml` or a separate `rustfs-buckets.yaml`.)

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

- **Removed**: `chart/templates/minio.yaml`, `chart/templates/postgres.yaml`.
- **Added**: `chart/templates/cnpg-cluster.yaml`,
  `chart/templates/rustfs-tenant.yaml` (+ optional `rustfs-buckets.yaml`),
  `chart/charts/rustfs-operator/` (vendored), `scripts/vendor-rustfs-operator.sh`.
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
  - Buckets created by the post-install Job.
  - Migrate Job succeeds (Alembic `upgrade head` against `rask-postgres-rw`).
  - `rask-gateway` rolls out; `/api/health` green.
  - S3 round-trip via `scripts/smoke_s3.py` against `rask-rustfs-io:9000`; a DB
    read confirms connectivity.

## Risks / to verify at implementation

- **RustFS operator image**: confirm a prebuilt operator image is published; if
  not, add a build-and-`k3s-import` step (consistent with the other `:dev` images).
- **RustFS `Tenant` spec**: pin exact Secret key names and Tenant fields
  (pools/persistence/credentials/ports) against the vendored chart's
  `values.yaml`/examples.
- **CNPG chart version** pin; confirm the operator installs cleanly into the
  `default` release namespace (KubeRay already does as a subchart).
- **CNPG webhook `failurePolicy`** confirmed `Fail` → the post-install-hook
  ordering is what makes the first install reliable.
- **Vendored chart upkeep**: the local `rustfs-operator` chart must be refreshed
  against upstream tags via `scripts/vendor-rustfs-operator.sh`; CRD upgrades are
  not auto-managed by Helm (`charts/*/crds/` are install-only).
