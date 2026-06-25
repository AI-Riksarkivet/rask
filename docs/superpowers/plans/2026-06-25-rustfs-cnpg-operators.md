# RustFS + CloudNativePG Operator Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rask Helm chart's hand-rolled MinIO and single-Postgres StatefulSets with operator-managed RustFS (S3) and CloudNativePG (Postgres), keeping one-command `make k3s-up` and the `*.enabled` gating.

**Architecture:** Two operators join the chart the way KubeRay already does — gated subchart dependencies whose custom resources (`Tenant`, `Cluster`) are plain `*.enabled`-gated templates. CNPG's admission webhook is set to `failurePolicy: Ignore` so a `Cluster` is admitted before the operator is Ready and reconciled later (no Helm hook, no data-loss hazard). RustFS provisions its buckets natively via `spec.buckets`. The app's connection points move to the operator-created services `rask-postgres-rw:5432` and `rask-rustfs-io:9000`.

**Tech Stack:** Helm 3 (subchart deps, `helm dependency build`), CloudNativePG operator chart `0.28.3` (appVersion 1.29.1), RustFS operator chart `0.1.0` (image `rustfs/operator:latest`, `Tenant` `rustfs.com/v1alpha1`), k3s + local-path storage.

## Global Constraints

- **Engineering principles (CLAUDE.md):** root-cause fixes, no band-aids; verify end-to-end like it ships; no silent scope-cuts.
- **No `Co-Authored-By: Claude` trailer** on any commit.
- **Chart fullname is pinned** `fullnameOverride: "rask"` → all object names are `rask-<component>`.
- **`chart/charts/` is gitignored** (rebuilt by `helm dependency build` from `Chart.lock`). Committed subchart source must live elsewhere; vendored RustFS operator goes in repo-root `third_party/rustfs-operator/`.
- **`secrets.postgresPassword` / `secrets.minioAccessKey` / `secrets.minioSecretKey` value keys stay** (the `Makefile` k3s-up `--set-string` and `.env` reference them) — only their consuming backends change.
- **RustFS creds minimum:** `accesskey` and `secretkey` must be ≥ 8 chars (operator validation).
- **RustFS erasure-coding minimum:** `servers * volumesPerServer >= 4`. Standalone = `servers:1, volumesPerServer:4`.
- **`services.*.waitFor` literal keys `"postgres"`/`"minio"` are kept** as logical dependency names; only the `nc` target hosts they map to change.
- Toggle independence mirrors KubeRay: `cnpg.enabled` / `rustfs.enabled` gate **both** the operator subchart (via `Chart.yaml` `condition:`) and the CR template (via `{{- if .Values.<k>.enabled }}`).

---

## File Structure

- `chart/Chart.yaml` — add `cloudnative-pg` (remote) + `rustfs-operator` (`file://`) deps.
- `chart/Chart.lock` — regenerated.
- `chart/values.yaml` — `postgres:`→`cnpg:`, `minio:`→`rustfs:`, operator-subchart override keys.
- `chart/templates/cnpg-cluster.yaml` — **new** CNPG `Cluster`.
- `chart/templates/rustfs-tenant.yaml` — **new** RustFS `Tenant` (with `spec.buckets`).
- `chart/templates/postgres.yaml`, `chart/templates/minio.yaml` — **deleted**.
- `chart/templates/secrets.yaml` — basic-auth PG secret; `rask-rustfs` creds secret; `rask-app` S3 endpoint.
- `chart/templates/_helpers.tpl` — `databaseUrl` host → `-postgres-rw`; lookups + access-key default.
- `chart/templates/fleet.yaml`, `chart/templates/migration-job.yaml` — `nc` target hosts + `cnpg.enabled`.
- `chart/templates/NOTES.txt`, `chart/README.md`, `CLAUDE.md`, `docs/architecture/deployment.md` — docs.
- `Makefile` — `K3S_DEP_REPOS` += cnpg.
- `third_party/rustfs-operator/` — **new** vendored operator chart.
- `scripts/vendor-rustfs-operator.sh` — **new** refresh script.

---

## Task 1: Operator dependencies + vendored RustFS chart

**Files:**
- Create: `scripts/vendor-rustfs-operator.sh`, `third_party/rustfs-operator/` (vendored)
- Modify: `chart/Chart.yaml`, `chart/Chart.lock` (regenerated), `chart/values.yaml`, `Makefile:270-274`

**Interfaces:**
- Produces: value toggles `cnpg.enabled` (default true) and `rustfs.enabled` (default true); operator subchart override keys `cloudnative-pg:` and `rustfs-operator:`. Built charts land in `chart/charts/` after `helm dependency build`.

- [ ] **Step 1: Write the vendoring script**

Create `scripts/vendor-rustfs-operator.sh`:

```bash
#!/usr/bin/env bash
# Vendors the RustFS operator Helm chart from github.com/rustfs/operator into
# third_party/rustfs-operator/ (chart/charts/ is gitignored, so the committed
# source lives here and helm dependency build packages it from a file:// repo).
set -euo pipefail
REF="${1:-main}"
DEST="third_party/rustfs-operator"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ">> cloning rustfs/operator @ ${REF}"
git clone --depth 1 --branch "$REF" https://github.com/rustfs/operator "$TMP/op" 2>/dev/null \
  || git clone "https://github.com/rustfs/operator" "$TMP/op"
( cd "$TMP/op" && git checkout "$REF" >/dev/null 2>&1 || true )
SHA="$(cd "$TMP/op" && git rev-parse HEAD)"

test -d "$TMP/op/deploy/rustfs-operator" || { echo "ERROR: deploy/rustfs-operator not found at $REF"; exit 1; }
rm -rf "$DEST"; mkdir -p "$DEST"
cp -R "$TMP/op/deploy/rustfs-operator/." "$DEST/"
printf 'ref: %s\ncommit: %s\n' "$REF" "$SHA" > "$DEST/.vendored-ref"
echo ">> vendored to ${DEST} (commit ${SHA}). Review 'git diff' before committing."
```

- [ ] **Step 2: Run it and verify the chart landed**

```bash
chmod +x scripts/vendor-rustfs-operator.sh
./scripts/vendor-rustfs-operator.sh main
test -f third_party/rustfs-operator/Chart.yaml && echo OK-chart
test -d third_party/rustfs-operator/crds && echo OK-crds
grep -q '^name: rustfs-operator' third_party/rustfs-operator/Chart.yaml && echo OK-name
```
Expected: `OK-chart`, `OK-crds`, `OK-name`. If the chart `version:` is not `0.1.0`, note the actual version for Step 4.

- [ ] **Step 3: Add the dependencies to `chart/Chart.yaml`**

Append to the `dependencies:` list (after the `openfga` entry):

```yaml
  - name: cloudnative-pg
    version: "0.28.3"
    repository: https://cloudnative-pg.github.io/charts
    condition: cnpg.enabled
  - name: rustfs-operator
    version: "0.1.0"
    repository: "file://../third_party/rustfs-operator"
    condition: rustfs.enabled
```

(Use the actual chart `version:` from Step 2 for `rustfs-operator` if it differs from `0.1.0`.)

- [ ] **Step 4: Add toggles + operator overrides to `chart/values.yaml`**

Add near the other infra blocks (e.g. after the `kuberay:` block):

```yaml
# cloudnative-pg operator subchart (Chart.yaml dep, gated by cnpg.enabled).
# Webhook failurePolicy=Ignore so a Cluster created before the operator pod is
# Ready is admitted and reconciled later (the KubeRay/RayService behaviour) —
# this is what lets the Cluster be a plain template instead of a hook.
cloudnative-pg:
  webhook:
    validating:
      failurePolicy: Ignore
    mutating:
      failurePolicy: Ignore

# rustfs operator subchart (vendored at third_party/, gated by rustfs.enabled).
rustfs-operator: {}
```

- [ ] **Step 5: Add cnpg repo to the Makefile dep list**

In `Makefile:270-274`, change the `openfga` line to keep the backslash and add cnpg:

```make
K3S_DEP_REPOS = nvdp=https://nvidia.github.io/k8s-device-plugin \
                kuberay=https://ray-project.github.io/kuberay-helm/ \
                nats=https://nats-io.github.io/k8s/helm/charts/ \
                dapr=https://dapr.github.io/helm-charts/ \
                openfga=https://openfga.github.io/helm-charts \
                cnpg=https://cloudnative-pg.github.io/charts
```

(`rustfs-operator` is a `file://` dep — no `helm repo add` needed.)

- [ ] **Step 6: Regenerate the lock + build deps**

Run:

```bash
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm repo update >/dev/null
helm dependency update ./chart
```
Expected: `Saving N charts` including `cloudnative-pg` and `rustfs-operator`; `chart/Chart.lock` now lists both; `chart/charts/` contains `cloudnative-pg-0.28.3.tgz` and `rustfs-operator-*.tgz`. If `file://../third_party/rustfs-operator` fails to resolve, confirm the path is relative to `chart/` and the dir has a valid `Chart.yaml`.

- [ ] **Step 7: Verify operators render**

```bash
helm template rask ./chart --set cnpg.enabled=true --set rustfs.enabled=true \
  | grep -E 'cnpg-controller-manager|rustfs-operator|kind: (Deployment|CustomResourceDefinition)' | head
```
Expected: the CNPG controller-manager Deployment and RustFS operator Deployment appear. Then confirm gating:

```bash
helm template rask ./chart --set cnpg.enabled=false --set rustfs.enabled=false \
  | grep -ciE 'cnpg-controller-manager|rustfs-operator' || echo "0 (disabled OK)"
```
Expected: `0 (disabled OK)`.

- [ ] **Step 8: Commit**

```bash
git add scripts/vendor-rustfs-operator.sh third_party/rustfs-operator chart/Chart.yaml chart/Chart.lock chart/values.yaml Makefile
git commit -m "feat(chart): add cloudnative-pg + rustfs operator subchart deps"
```

---

## Task 2: CloudNativePG Cluster replaces the Postgres StatefulSet

**Files:**
- Create: `chart/templates/cnpg-cluster.yaml`
- Delete: `chart/templates/postgres.yaml`
- Modify: `chart/values.yaml` (`postgres:`→`cnpg:`), `chart/templates/secrets.yaml`, `chart/templates/_helpers.tpl`, `chart/templates/fleet.yaml`, `chart/templates/migration-job.yaml`

**Interfaces:**
- Consumes: `cnpg.enabled`, `rask.fullname` (Task 1 / existing helpers).
- Produces: CNPG `Cluster` `rask-postgres` → primary service `rask-postgres-rw:5432`; basic-auth Secret `rask-postgres` (keys `username`/`password`); `rask.databaseUrl` → `postgresql+asyncpg://rask:<pw>@rask-postgres-rw:5432/rask`.

- [ ] **Step 1: Replace the `postgres:` values block with `cnpg:`**

In `chart/values.yaml`, replace the entire `postgres:` block with:

```yaml
# In-cluster Postgres via the CloudNativePG operator (Cluster CR + operator
# subchart). `enabled` gates BOTH the operator subchart (Chart.yaml condition
# cnpg.enabled) and the Cluster template. Turn off for an external/managed DB
# (provide DATABASE_URL via existingSecret).
cnpg:
  enabled: true
  instances: 1
  imageName: "ghcr.io/cloudnative-pg/postgresql:16"
  user: "rask"
  database: "rask"
  port: 5432
  storage: "8Gi"
  storageClass: "local-path"
  resources:
    requests: {cpu: "100m", memory: "256Mi"}
    limits: {cpu: "1", memory: "1Gi"}
```

- [ ] **Step 2: Write a render assertion (expected to fail)**

```bash
helm template rask ./chart --show-only templates/cnpg-cluster.yaml 2>&1 | grep -E 'kind: Cluster'
```
Expected: FAIL — `could not find template templates/cnpg-cluster.yaml` (file doesn't exist yet).

- [ ] **Step 3: Create `chart/templates/cnpg-cluster.yaml`**

```yaml
{{- if .Values.cnpg.enabled }}
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: {{ include "rask.fullname" . }}-postgres
  labels:
    {{- include "rask.componentLabels" (list . "postgres") | nindent 4 }}
spec:
  instances: {{ .Values.cnpg.instances }}
  imageName: {{ .Values.cnpg.imageName | quote }}
  bootstrap:
    initdb:
      database: {{ .Values.cnpg.database | quote }}
      owner: {{ .Values.cnpg.user | quote }}
      secret:
        name: {{ include "rask.fullname" . }}-postgres
  storage:
    size: {{ .Values.cnpg.storage | quote }}
    storageClass: {{ .Values.cnpg.storageClass | quote }}
  resources:
    {{- toYaml .Values.cnpg.resources | nindent 4 }}
{{- end }}
```

- [ ] **Step 4: Replace the Postgres Secret with a basic-auth Secret**

In `chart/templates/secrets.yaml`, replace the `{{- if .Values.postgres.enabled }}` … secret block with:

```yaml
{{- if .Values.cnpg.enabled }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-postgres
  labels:
    {{- include "rask.componentLabels" (list . "postgres") | nindent 4 }}
type: kubernetes.io/basic-auth
stringData:
  username: {{ .Values.cnpg.user | quote }}
  password: {{ include "rask.pgPassword" . | quote }}
{{- end }}
```

- [ ] **Step 5: Update `_helpers.tpl` — pgPassword lookup + databaseUrl host**

In `chart/templates/_helpers.tpl`, in `rask.pgPassword`, change the lookup-data index from `POSTGRES_PASSWORD` to `password`:

```
{{- if and $existing $existing.data (index $existing.data "password") -}}
{{- index $existing.data "password" | b64dec -}}
```

And replace `rask.databaseUrl` with (host `-postgres` → `-postgres-rw`, `.Values.postgres.*` → `.Values.cnpg.*`):

```
{{- define "rask.databaseUrl" -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgres-rw:%v/%s" .Values.cnpg.user (include "rask.pgPassword" .) (include "rask.fullname" .) .Values.cnpg.port .Values.cnpg.database -}}
{{- end -}}
```

- [ ] **Step 6: Update `fleet.yaml` + `migration-job.yaml` postgres wait targets**

In `chart/templates/fleet.yaml`, the `wait-postgres` initContainer command:

```
command: ["sh","-c","until nc -z {{ include "rask.fullname" $root }}-postgres-rw {{ $root.Values.cnpg.port }}; do echo waiting postgres; sleep 2; done"]
```

In `chart/templates/migration-job.yaml`, change `{{- if .Values.postgres.enabled }}` → `{{- if .Values.cnpg.enabled }}`, and the `wait-postgres` command:

```
command: ["sh","-c","until nc -z {{ include "rask.fullname" . }}-postgres-rw {{ .Values.cnpg.port }}; do echo waiting postgres; sleep 2; done"]
```

- [ ] **Step 7: Delete the old StatefulSet template**

```bash
git rm chart/templates/postgres.yaml
```

- [ ] **Step 8: Run assertions (expected to pass)**

```bash
helm template rask ./chart --set secrets.postgresPassword=testpw123 \
  --show-only templates/cnpg-cluster.yaml | grep -E 'kind: Cluster|name: rask-postgres-rw|owner: "rask"' ; \
helm template rask ./chart --set secrets.postgresPassword=testpw123 \
  | grep -E 'postgresql\+asyncpg://rask:testpw123@rask-postgres-rw:5432/rask' && echo OK-URL ; \
helm template rask ./chart | grep -c 'kind: StatefulSet' | grep -qv 0 && echo "still has STS (expect only minio left)" || true
```
Expected: `Cluster` kind present with `owner: "rask"`; `OK-URL`; no postgres StatefulSet (only the not-yet-removed minio one remains). Run `helm lint ./chart` → 0 errors.

- [ ] **Step 9: Commit**

```bash
git add chart/values.yaml chart/templates/cnpg-cluster.yaml chart/templates/secrets.yaml chart/templates/_helpers.tpl chart/templates/fleet.yaml chart/templates/migration-job.yaml
git rm chart/templates/postgres.yaml
git commit -m "feat(chart): replace Postgres StatefulSet with CloudNativePG Cluster"
```

---

## Task 3: RustFS Tenant replaces the MinIO StatefulSet + bucket Job

**Files:**
- Create: `chart/templates/rustfs-tenant.yaml`
- Delete: `chart/templates/minio.yaml`
- Modify: `chart/values.yaml` (`minio:`→`rustfs:`), `chart/templates/secrets.yaml`, `chart/templates/_helpers.tpl`, `chart/templates/fleet.yaml`

**Interfaces:**
- Consumes: `rustfs.enabled`, `rask.minioAccessKey`, `rask.minioSecretKey`, `rask.fullname`.
- Produces: RustFS `Tenant` `rask-rustfs` → S3 service `rask-rustfs-io:9000`; Secret `rask-rustfs` (keys `accesskey`/`secretkey`); buckets `images-batch`, `images-batch-alto`, `images-batch-search`; `rask-app` `HCP_ENDPOINT=http://rask-rustfs-io:9000`.

- [ ] **Step 1: Replace the `minio:` values block with `rustfs:`**

In `chart/values.yaml`, replace the entire `minio:` block with:

```yaml
# In-cluster S3 object store via the RustFS operator (Tenant CR + operator
# subchart). `enabled` gates BOTH the operator subchart (Chart.yaml condition
# rustfs.enabled) and the Tenant template. Turn off for external S3.
# RustFS erasure coding requires servers*volumesPerServer >= 4; standalone is
# servers=1, volumesPerServer=4 (1 pod, 4 PVCs). S3 service is <tenant>-io:9000.
rustfs:
  enabled: true
  image: "rustfs/rustfs:latest"
  servers: 1
  volumesPerServer: 4
  port: 9000
  consolePort: 9001
  storage: "15Gi"          # per-volume; 4 volumes => ~60Gi total (local-path is unmetered hostPath)
  storageClass: "local-path"
  buckets: ["images-batch", "images-batch-alto", "images-batch-search"]
```

- [ ] **Step 2: Raise the default access key to ≥ 8 chars in `_helpers.tpl`**

The operator rejects an `accesskey` shorter than 8 chars; the current default `"rask"` is 4. In `chart/templates/_helpers.tpl`, change `rask.minioAccessKey`:

```
{{- define "rask.minioAccessKey" -}}
{{- default "raskadmin" .Values.secrets.minioAccessKey -}}
{{- end -}}
```

And update `rask.minioSecretKey`'s lookup to the new Secret/key (`rask-rustfs` / `secretkey`, replacing `rask-minio` / `MINIO_ROOT_PASSWORD`):

```
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-rustfs" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "secretkey") -}}
{{- index $existing.data "secretkey" | b64dec -}}
```

- [ ] **Step 3: Write a render assertion (expected to fail)**

```bash
helm template rask ./chart --show-only templates/rustfs-tenant.yaml 2>&1 | grep -E 'kind: Tenant'
```
Expected: FAIL — template not found.

- [ ] **Step 4: Create `chart/templates/rustfs-tenant.yaml`**

```yaml
{{- if .Values.rustfs.enabled }}
apiVersion: rustfs.com/v1alpha1
kind: Tenant
metadata:
  name: {{ include "rask.fullname" . }}-rustfs
  labels:
    {{- include "rask.componentLabels" (list . "rustfs") | nindent 4 }}
spec:
  image: {{ .Values.rustfs.image | quote }}
  credsSecret:
    name: {{ include "rask.fullname" . }}-rustfs
  pools:
    - name: pool-0
      servers: {{ .Values.rustfs.servers }}
      persistence:
        volumesPerServer: {{ .Values.rustfs.volumesPerServer }}
        volumeClaimTemplate:
          accessModes: ["ReadWriteOnce"]
          resources:
            requests:
              storage: {{ .Values.rustfs.storage | quote }}
          storageClassName: {{ .Values.rustfs.storageClass | quote }}
  buckets:
    {{- range .Values.rustfs.buckets }}
    - name: {{ . | quote }}
    {{- end }}
{{- end }}
```

- [ ] **Step 5: Replace the MinIO Secret with the RustFS creds Secret**

In `chart/templates/secrets.yaml`, replace the `{{- if .Values.minio.enabled }}` … secret block with:

```yaml
{{- if .Values.rustfs.enabled }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-rustfs
  labels:
    {{- include "rask.componentLabels" (list . "rustfs") | nindent 4 }}
type: Opaque
stringData:
  accesskey: {{ include "rask.minioAccessKey" . | quote }}
  secretkey: {{ include "rask.minioSecretKey" . | quote }}
{{- end }}
```

- [ ] **Step 6: Repoint the app S3 endpoint in the `rask-app` Secret**

In `chart/templates/secrets.yaml`, change the `HCP_ENDPOINT` line:

```yaml
  HCP_ENDPOINT: {{ printf "http://%s-rustfs-io:%v" (include "rask.fullname" .) .Values.rustfs.port | quote }}
```

- [ ] **Step 7: Update the `minio` wait target in `fleet.yaml`**

In `chart/templates/fleet.yaml`, the `wait-minio` initContainer command (the `waitFor` literal key stays `"minio"`):

```
command: ["sh","-c","until nc -z {{ include "rask.fullname" $root }}-rustfs-io {{ $root.Values.rustfs.port }}; do echo waiting s3; sleep 2; done"]
```

- [ ] **Step 8: Delete the old MinIO template (StatefulSet + Service + bucket Job)**

```bash
git rm chart/templates/minio.yaml
```

- [ ] **Step 9: Run assertions (expected to pass)**

```bash
helm template rask ./chart --set secrets.postgresPassword=testpw123 \
  --show-only templates/rustfs-tenant.yaml \
  | grep -E 'kind: Tenant|volumesPerServer: 4|name: "images-batch"' && echo OK-tenant ; \
helm template rask ./chart --set secrets.postgresPassword=testpw123 \
  | grep -E 'HCP_ENDPOINT: "http://rask-rustfs-io:9000"' && echo OK-endpoint ; \
helm template rask ./chart --set secrets.postgresPassword=testpw123 \
  | grep -c 'kind: StatefulSet' | grep -q '^0$' && echo "OK-no-STS"
```
Expected: `OK-tenant`, `OK-endpoint`, `OK-no-STS` (both hand-rolled StatefulSets now gone). `helm lint ./chart` → 0 errors.

- [ ] **Step 10: Commit**

```bash
git add chart/values.yaml chart/templates/rustfs-tenant.yaml chart/templates/secrets.yaml chart/templates/_helpers.tpl chart/templates/fleet.yaml
git rm chart/templates/minio.yaml
git commit -m "feat(chart): replace MinIO StatefulSet with RustFS operator Tenant"
```

---

## Task 4: NOTES, docs, and the prod (external-infra) render path

**Files:**
- Modify: `chart/templates/NOTES.txt`, `chart/README.md`, `CLAUDE.md`, `docs/architecture/deployment.md`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: docs consistent with the new backends; verified `*.enabled=false` render.

- [ ] **Step 1: Update `chart/templates/NOTES.txt`**

Read the file; replace any `minio`/`postgres` service references with `rask-rustfs-io:9000` (S3) / `rask-rustfs-console:9001` (console) and `rask-postgres-rw:5432` (DB). Keep the existing structure; only the names/endpoints change.

- [ ] **Step 2: Update `chart/README.md`**

Replace the MinIO/Postgres descriptions with the operator-backed ones: CNPG `Cluster` (`cnpg.*` values, `rask-postgres-rw`), RustFS `Tenant` (`rustfs.*` values, `rask-rustfs-io`, native `spec.buckets`, 1 pod/4 PVCs). Document that `cnpg.enabled`/`rustfs.enabled` gate both operator + CR, and that the RustFS operator is vendored via `scripts/vendor-rustfs-operator.sh`.

- [ ] **Step 3: Update `CLAUDE.md` deployment paragraph**

In the **State surface** bullet, change "in-cluster Postgres, MinIO, and KubeRay are gated by `postgres.enabled`/`minio.enabled`/`ray.enabled`" to reflect CNPG + RustFS operators gated by `cnpg.enabled`/`rustfs.enabled`/`ray.enabled`, with services `rask-postgres-rw` / `rask-rustfs-io`.

- [ ] **Step 4: Update `docs/architecture/deployment.md`**

Read the file; update the in-cluster Postgres/MinIO sections to CNPG `Cluster` + RustFS `Tenant`, the new service names, the vendored operator note, and the `make k3s-purge && make k3s-up` greenfield cutover.

- [ ] **Step 5: Verify the external-infra (operators off) path renders**

```bash
helm template rask ./chart --set cnpg.enabled=false --set rustfs.enabled=false \
  --set existingSecret=rask-external 2>&1 | grep -ciE 'kind: (Cluster|Tenant)' | grep -q '^0$' && echo OK-external
helm lint ./chart
```
Expected: `OK-external`; lint passes. (With `existingSecret` set, the chart-generated `rask-app` secret is skipped and no CR/operator renders.)

- [ ] **Step 6: Commit**

```bash
git add chart/templates/NOTES.txt chart/README.md CLAUDE.md docs/architecture/deployment.md
git commit -m "docs(chart): document RustFS + CloudNativePG operator backends"
```

---

## Task 5: Live greenfield verification on k3s

**Files:** none (verification + any fixups discovered).

**Interfaces:** Consumes the full chart from Tasks 1–4.

- [ ] **Step 1: Lint + full render matrix**

```bash
helm lint ./chart
helm template rask ./chart --set secrets.postgresPassword=testpw123 >/dev/null && echo OK-render-on
helm template rask ./chart --set cnpg.enabled=false --set rustfs.enabled=false --set existingSecret=x >/dev/null && echo OK-render-off
```
Expected: lint clean; `OK-render-on`; `OK-render-off`.

- [ ] **Step 2: Greenfield teardown (drops old MinIO/Postgres PVCs)**

```bash
make k3s-down || true
kubectl delete pvc -l app.kubernetes.io/component=minio --ignore-not-found
kubectl delete pvc -l app.kubernetes.io/component=postgres --ignore-not-found
make k3s-purge || true
```
Expected: old `rask-minio` / `rask-postgres` PVCs gone (`kubectl get pvc` shows none for those components).

- [ ] **Step 3: Vendor deps + bring the release up**

```bash
make k3s-deps
make k3s-up
```
Expected: `helm dependency build` vendors cnpg + rustfs-operator; `helm upgrade --install ... --wait` completes; `rask-gateway` rollout succeeds.

- [ ] **Step 4: Verify operators + CRs reconciled**

```bash
kubectl get cluster rask-postgres -o jsonpath='{.status.phase}{"\n"}'        # expect: Cluster in healthy state
kubectl get tenant rask-rustfs -o jsonpath='{.status.conditions[*].type}{"\n"}'  # expect: Ready/Progressing
kubectl get svc rask-postgres-rw rask-rustfs-io
kubectl get pods -l app.kubernetes.io/component=postgres
```
Expected: `rask-postgres` cluster healthy (1 instance); `rask-rustfs` tenant Ready; both services present.

- [ ] **Step 5: Verify buckets exist (operator-provisioned)**

```bash
ACCESS=$(kubectl get secret rask-rustfs -o jsonpath='{.data.accesskey}' | base64 -d)
SECRET=$(kubectl get secret rask-rustfs -o jsonpath='{.data.secretkey}' | base64 -d)
kubectl run s3check --rm -it --restart=Never --image=amazon/aws-cli --env AWS_ACCESS_KEY_ID="$ACCESS" --env AWS_SECRET_ACCESS_KEY="$SECRET" -- \
  --endpoint-url http://rask-rustfs-io:9000 s3 ls
```
Expected: lists `images-batch`, `images-batch-alto`, `images-batch-search`. **If buckets are missing** (operator `spec.buckets` not yet reconciling): fall back to a post-install `aws s3api create-bucket` Job against `rask-rustfs-io:9000` (see spec Risks) and re-verify.

- [ ] **Step 6: Verify migrations + app health end-to-end**

```bash
kubectl get job rask-migrate -o jsonpath='{.status.succeeded}{"\n"}'   # expect: 1
kubectl run pgcheck --rm -it --restart=Never --image=postgres:16 -- \
  psql "$(kubectl get secret rask-app -o jsonpath='{.data.DATABASE_URL}' | base64 -d | sed 's#postgresql+asyncpg#postgresql#')" -c '\dt' | head
curl -fsS http://localhost/api/health || kubectl port-forward svc/rask-gateway 8888:8888 & sleep 3; curl -fsS http://localhost:8888/api/health
```
Expected: migrate Job `succeeded: 1`; `\dt` lists the alembic-created tables; `/api/health` returns 200.

- [ ] **Step 7: S3 round-trip smoke**

```bash
uv run python components/scripts/smoke_s3.py   # uses RASK_S3_ENDPOINT_URL/HCP_ENDPOINT + AWS creds
```
Expected: a put/get/list round-trip against `rask-rustfs-io:9000` succeeds. (Run from a pod or with the endpoint port-forwarded if outside the cluster.)

- [ ] **Step 8: Commit any fixups**

```bash
git add -A && git commit -m "fix(chart): adjustments from live k3s verification"   # only if changes were needed
```

---

## Self-Review

- **Spec coverage:** deps (T1) · CNPG Cluster + secret + wiring (T2) · RustFS Tenant + buckets + secret + endpoint (T3) · NOTES/README/CLAUDE/deployment + external-infra path (T4) · greenfield live verify incl. buckets/migrations/health/S3 (T5). All spec sections map to a task.
- **Placeholder scan:** every step has concrete code/commands and expected output; no TBD/TODO.
- **Type/name consistency:** `rask-postgres`/`rask-postgres-rw`, `rask-rustfs`/`rask-rustfs-io`, secret keys `username`/`password` (PG basic-auth) and `accesskey`/`secretkey` (RustFS), value keys `cnpg.*`/`rustfs.*` used identically across tasks; `secrets.postgresPassword`/`minioAccessKey`/`minioSecretKey` unchanged throughout.
