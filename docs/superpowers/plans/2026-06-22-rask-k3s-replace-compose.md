# rask: replace docker-compose with local k3s — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the full rask microservice fleet + in-cluster Postgres, MinIO, and a KubeRay-managed GPU htrflow Serve endpoint to a local single-node k3s cluster via one Helm chart, then remove docker-compose.

**Architecture:** Rewrite the existing `chart/` from the stale single-`viewer` topology to the fleet topology (gateway, core-api, search-api, volumes-api, ray-api, orchestrator) with in-cluster Postgres/MinIO StatefulSets and a KubeRay `RayService` CRD, all gated by `*.enabled` toggles so the chart still serves prod with external deps. Images are built locally and side-loaded with `k3s ctr images import`. Traefik (bundled with k3s) routes `/` → frontend and `/api` → gateway. New `make k3s-*` targets drive install/build/import/up/down.

**Tech Stack:** Helm 3, k3s (bundled containerd + Traefik), KubeRay operator + RayService CRD, NVIDIA k8s device-plugin, MinIO, Postgres 16, existing `.docker/*.dockerfile` images, GNU Make.

## Global Constraints

- **Helm release name is pinned to `rask`**, and `values.yaml` sets `fullnameOverride: "rask"`, so all in-cluster service names are `rask-<component>` (e.g. `rask-gateway`, `rask-postgres`, `rask-minio`). Templates MUST reference services via `{{ include "rask.fullname" . }}-<component>`.
- **API prefix is `/api`** (not `/api/v1`). All backend health probes hit `/api/health`; the gateway docs are at `/api/docs`.
- **Service ports (container):** gateway 8888, core-api 8801, search-api 8802, volumes-api 8803, ray-api 8804, orchestrator 8810, frontend **3000**, postgres 5432, minio 9000.
- **The orchestrator is an in-process singleton:** `replicas: 1` + `strategy: { type: Recreate }`, and it owns `RASK_ORCHESTRATOR_AUTOSTART` (default `"false"`). Never scale it >1.
- **Image tags are `:dev`** with `pullPolicy: IfNotPresent` (side-loaded, never pulled).
- **No Claude/AI co-author trailer** on any commit. Work stays on branch `local-k3s-replace-compose`.
- **htrflow Serve app** is importable as `runner.htrflow_service:htrflow_app`, route prefix `/htrflow`, parametrized by env `RASK_SERVE_REPLICAS` / `RASK_SERVE_GPU_FRAC`.
- **GPU base image is already proven:** `ray.dockerfile` on `nvidia/cuda:13.0.1-runtime-ubuntu24.04` (aarch64/GB10). No image spike.
- Pinned external versions: KubeRay operator `1.4.2`, NVIDIA k8s device-plugin `v0.17.4`.
- After every template change, the verification gate is `helm lint chart/` clean AND `helm template rask ./chart` renders without error. Use `grep` (not `yq`) in checks for portability.

---

## File Structure

**Chart (`chart/`):**
- `chart/Chart.yaml` — modify description (fleet, not viewer).
- `chart/values.yaml` — full rewrite: `fullnameOverride`, `services.*` map, `postgres`, `minio`, `ray`, `frontend`, `ingress`, `config`, `migrations`, `secrets` blocks.
- `chart/templates/_helpers.tpl` — add `rask.componentLabels`, `rask.pgPassword`, `rask.minioAccessKey`, `rask.minioSecretKey`, `rask.databaseUrl` helpers.
- `chart/templates/secrets.yaml` — **create**: app Secret (DATABASE_URL, AWS_*, HCP_ENDPOINT, HF_TOKEN) + Postgres Secret + MinIO Secret, all with lookup-pinned random generation.
- `chart/templates/configmap.yaml` — modify: fleet env (gateway upstreams, API prefix, source/pipeline flags).
- `chart/templates/postgres.yaml` — **create**: StatefulSet + Service (gated by `postgres.enabled`).
- `chart/templates/minio.yaml` — **create**: StatefulSet + Service + buckets Job (gated by `minio.enabled`).
- `chart/templates/fleet.yaml` — **create**: ranged Deployment+Service per entry in `.Values.services`.
- `chart/templates/frontend-deployment.yaml` + `frontend-service.yaml` — modify: port 3000, ORIGIN/RASK_GATEWAY_URL env.
- `chart/templates/migration-job.yaml` — modify: core-api image, chart-generated secret.
- `chart/templates/rayservice.yaml` — **create**: KubeRay RayService (gated by `ray.enabled`).
- `chart/templates/ingress.yaml` — modify: `/api`→gateway, `/`→frontend.
- `chart/templates/NOTES.txt` — modify: fleet + URLs.
- **Delete:** `chart/templates/viewer-deployment.yaml`, `chart/templates/viewer-service.yaml`.

**Cluster tooling:**
- `scripts/k3s-install.sh` — **create**: install k3s + helm + NVIDIA device-plugin + KubeRay operator.
- `Makefile` — add `k3s-install`, `k3s-build`, `k3s-import`, `k3s-up`, `k3s-down`, `k3s-purge`; remove `compose-*` targets in the final task.

**Removed in final task:**
- `docker-compose.yml`, `.docker/ingress.Caddyfile`, `.docker/smoke-compose.sh`.

**Docs:**
- `README.md`, `CLAUDE.md`, `docs/architecture/deployment.md` — update to k3s flow.

---

## Task 1: Chart scaffolding — values rewrite, helpers, drop viewer

**Files:**
- Modify: `chart/Chart.yaml`
- Modify: `chart/values.yaml` (full rewrite)
- Modify: `chart/templates/_helpers.tpl`
- Delete: `chart/templates/viewer-deployment.yaml`, `chart/templates/viewer-service.yaml`

**Interfaces:**
- Produces: `.Values.fullnameOverride="rask"`; `.Values.services` map (keys `core-api`,`search-api`,`volumes-api`,`ray-api`,`orchestrator`,`gateway`); `.Values.postgres`, `.Values.minio`, `.Values.ray`, `.Values.frontend`, `.Values.config`, `.Values.migrations`, `.Values.secrets`. Helper `rask.componentLabels` (args: list `[. , "<component>"]`).

- [ ] **Step 1: Write the failing test**

```bash
# Expect: lint passes AND no viewer resources remain AND services map renders gateway.
helm template rask ./chart 2>&1 | grep -q "kind: Deployment" \
  && helm template rask ./chart 2>&1 | grep -qi "viewer" && echo "FAIL: viewer still present" \
  || echo "viewer gone (good if no FAIL above)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/morgan/rask-main && helm template rask ./chart 2>&1 | grep -ci viewer`
Expected: a non-zero count (current chart still renders `viewer`), confirming the starting state.

- [ ] **Step 3: Delete the viewer templates**

```bash
cd /home/morgan/rask-main
git rm chart/templates/viewer-deployment.yaml chart/templates/viewer-service.yaml
```

- [ ] **Step 4: Rewrite `chart/Chart.yaml` description**

```yaml
apiVersion: v2
name: rask
description: rask HTR pipeline — microservice fleet + optional in-cluster Postgres, MinIO, and KubeRay Serve (Swedish National Archives)
type: application
version: 0.2.0
appVersion: "0.2.0"
home: https://github.com/AI-Riksarkivet/rask
maintainers:
  - name: Riksarkivet
```

- [ ] **Step 5: Rewrite `chart/values.yaml`**

```yaml
# Pinned so in-cluster service names are rask-<component> (release name is also rask).
fullnameOverride: "rask"
nameOverride: ""

imagePullSecrets: []

serviceAccount:
  create: true
  name: ""
  annotations: {}

# Optional override: if set, this Secret is used verbatim instead of the
# chart-generated one (prod with external deps). Empty = chart generates creds.
existingSecret: ""

# Random creds are generated once on first install and pinned across upgrades
# via `lookup`. Override here to force a value.
secrets:
  postgresPassword: ""   # empty => randAlphaNum 24, pinned
  minioAccessKey: ""     # empty => "rask"
  minioSecretKey: ""     # empty => randAlphaNum 24, pinned
  hfToken: ""            # HuggingFace token for the gated TrOCR model

# Non-secret env shared by the fleet ConfigMap.
config:
  RASK_SOURCE_MODE: "s3"
  RASK_HTR_PIPELINE: "htrflow"
  RASK_PREFETCH_PIPELINE: "none"
  RASK_VIEWER_INPUT: "s3://images-batch"
  RASK_VIEWER_OUTPUT: "s3://images-batch-alto"
  RASK_CACHE_BUCKET: "images-batch"
  RASK_OUTPUT_BUCKET: "images-batch-alto"
  RASK_SEARCH_BUCKET: "images-batch-search"
  RASK_IIIF_URL: ""
  RASK_API_PREFIX: "/api"
  RASK_ORCHESTRATOR_INTERVAL_SECONDS: "60"
  RASK_ORCHESTRATOR_RECONCILE_SECONDS: "600"
  RASK_SERVE_REPLICAS: "1"
  RASK_SERVE_GPU_FRAC: "1.0"
  AWS_REGION: "us-east-1"
  HCP_INSECURE: "true"
  RAY_ENABLE_UV_RUN_RUNTIME_ENV: "0"

# Per-service fleet definitions. Rendered by templates/fleet.yaml.
services:
  core-api:
    module: "core_api:app"
    port: 8801
    replicas: 1
    waitFor: ["postgres", "minio"]
  search-api:
    module: "search_api:app"
    port: 8802
    replicas: 1
    waitFor: ["postgres"]
  volumes-api:
    module: "volumes_api:app"
    port: 8803
    replicas: 1
    waitFor: ["minio"]
  ray-api:
    module: "ray_api:app"
    port: 8804
    replicas: 1
    waitFor: []
  orchestrator:
    module: "orchestrator:app"
    port: 8810
    replicas: 1            # MUST stay 1 (in-process singleton)
    singleton: true        # => strategy Recreate + RASK_ORCHESTRATOR_AUTOSTART
    waitFor: ["postgres"]
  gateway:
    module: "gateway:app"
    port: 8888
    replicas: 1
    extraArgs: ["--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
    waitFor: []

# Image registry/tag shared by every fleet + migration container.
image:
  repository: ""          # empty => bare "<component>" name (e.g. core-api)
  tag: "dev"
  pullPolicy: IfNotPresent

resources:
  fleet:
    requests: {cpu: "50m", memory: "128Mi"}
    limits: {cpu: "1", memory: "512Mi"}

orchestrator:
  autostart: "false"

frontend:
  replicas: 2
  image:
    repository: frontend
    tag: "dev"
    pullPolicy: IfNotPresent
  service:
    port: 3000
  origin: "http://rask.local"
  resources:
    requests: {cpu: "50m", memory: "64Mi"}
    limits: {cpu: "500m", memory: "256Mi"}
  podSecurityContext:
    runAsNonRoot: true
  securityContext:
    allowPrivilegeEscalation: false
    capabilities: {drop: ["ALL"]}

postgres:
  enabled: true
  image: "postgres:16"
  user: "rask"
  database: "rask"
  port: 5432
  storage: "8Gi"
  storageClass: "local-path"
  resources:
    requests: {cpu: "100m", memory: "256Mi"}
    limits: {cpu: "1", memory: "1Gi"}

minio:
  enabled: true
  image: "minio/minio:latest"
  mcImage: "minio/mc:latest"
  port: 9000
  consolePort: 9001
  storage: "50Gi"
  storageClass: "local-path"
  buckets: ["images-batch", "images-batch-alto", "images-batch-search"]
  resources:
    requests: {cpu: "100m", memory: "256Mi"}
    limits: {cpu: "1", memory: "1Gi"}

ray:
  enabled: true
  image:
    repository: ray
    tag: "dev"
    pullPolicy: IfNotPresent
  serveRoutePrefix: "/htrflow"
  importPath: "runner.htrflow_service:htrflow_app"
  dashboardPort: 8265
  servePort: 8000
  clientPort: 10001
  redisPort: 6379
  gpuCount: 1
  runtimeClassName: "nvidia"
  hfCacheStorage: "10Gi"
  shmSize: "8Gi"
  resources:
    requests: {cpu: "2", memory: "8Gi"}
    limits: {cpu: "4", memory: "16Gi"}

migrations:
  enabled: true
  command: ["sh", "-c", "cd /app/components/services/core && alembic upgrade head"]
  resources:
    requests: {cpu: "100m", memory: "256Mi"}
    limits: {cpu: "1", memory: "1Gi"}

ingress:
  enabled: true
  className: ""    # k3s default Traefik
  host: rask.local
  annotations: {}
  tls: []
```

- [ ] **Step 6: Add helpers to `chart/templates/_helpers.tpl`**

Append (keep existing helpers intact):

```yaml
{{/* Component labels: pass (list . "<component>") */}}
{{- define "rask.componentLabels" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{ include "rask.labels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end -}}

{{/* Postgres password: pinned across upgrades via lookup, else random. */}}
{{- define "rask.pgPassword" -}}
{{- if .Values.secrets.postgresPassword -}}
{{- .Values.secrets.postgresPassword -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "POSTGRES_PASSWORD") -}}
{{- index $existing.data "POSTGRES_PASSWORD" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "rask.minioAccessKey" -}}
{{- default "rask" .Values.secrets.minioAccessKey -}}
{{- end -}}

{{- define "rask.minioSecretKey" -}}
{{- if .Values.secrets.minioSecretKey -}}
{{- .Values.secrets.minioSecretKey -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-minio" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "MINIO_ROOT_PASSWORD") -}}
{{- index $existing.data "MINIO_ROOT_PASSWORD" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* asyncpg DATABASE_URL pointing at the in-cluster postgres service. */}}
{{- define "rask.databaseUrl" -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgres:%v/%s" .Values.postgres.user (include "rask.pgPassword" .) (include "rask.fullname" .) .Values.postgres.port .Values.postgres.database -}}
{{- end -}}
```

- [ ] **Step 7: Run lint + render**

Run: `cd /home/morgan/rask-main && helm lint chart/ && helm template rask ./chart >/tmp/render.yaml 2>&1; echo "exit=$?"`
Expected: lint reports `1 chart(s) linted, 0 chart(s) failed`. `helm template` may still fail because `configmap.yaml`/`ingress.yaml` reference removed `.Values.viewer` — that is fixed in later tasks. Acceptable for THIS task: confirm `helm lint` passes and no template references `viewer-deployment`. Run:
`grep -c viewer /tmp/render.yaml || true` → the only remaining references are in `ingress.yaml`/`NOTES.txt`/`configmap.yaml` (fixed next).

- [ ] **Step 8: Commit**

```bash
cd /home/morgan/rask-main
git add chart/Chart.yaml chart/values.yaml chart/templates/_helpers.tpl
git add -u chart/templates/
git commit -m "chart: rewrite values + helpers for fleet topology, drop viewer templates"
```

---

## Task 2: Secrets + ConfigMap

**Files:**
- Create: `chart/templates/secrets.yaml`
- Modify: `chart/templates/configmap.yaml`

**Interfaces:**
- Consumes: helpers `rask.pgPassword`, `rask.minioAccessKey`, `rask.minioSecretKey`, `rask.databaseUrl` (Task 1).
- Produces: Secret `rask-app` (keys `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `HCP_ENDPOINT`, `HF_TOKEN`); Secret `rask-postgres` (`POSTGRES_USER`,`POSTGRES_PASSWORD`,`POSTGRES_DB`); Secret `rask-minio` (`MINIO_ROOT_USER`,`MINIO_ROOT_PASSWORD`); ConfigMap `rask-config`. Fleet containers `envFrom` ConfigMap `rask-config` + Secret (`existingSecret` if set, else `rask-app`).

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/secrets.yaml 2>&1 | grep -q "name: rask-app" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Run the Step 1 command.
Expected: `FAIL` (template does not exist yet; helm errors with "could not find template").

- [ ] **Step 3: Create `chart/templates/secrets.yaml`**

```yaml
{{- /* App secret is generated only when the operator did not supply existingSecret. */}}
{{- if not .Values.existingSecret }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-app
  labels:
    {{- include "rask.labels" . | nindent 4 }}
type: Opaque
stringData:
  DATABASE_URL: {{ include "rask.databaseUrl" . | quote }}
  AWS_ACCESS_KEY_ID: {{ include "rask.minioAccessKey" . | quote }}
  AWS_SECRET_ACCESS_KEY: {{ include "rask.minioSecretKey" . | quote }}
  HCP_ENDPOINT: {{ printf "http://%s-minio:%v" (include "rask.fullname" .) .Values.minio.port | quote }}
  HF_TOKEN: {{ .Values.secrets.hfToken | quote }}
{{- end }}
{{- if .Values.postgres.enabled }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-postgres
  labels:
    {{- include "rask.componentLabels" (list . "postgres") | nindent 4 }}
type: Opaque
stringData:
  POSTGRES_USER: {{ .Values.postgres.user | quote }}
  POSTGRES_PASSWORD: {{ include "rask.pgPassword" . | quote }}
  POSTGRES_DB: {{ .Values.postgres.database | quote }}
{{- end }}
{{- if .Values.minio.enabled }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ include "rask.fullname" . }}-minio
  labels:
    {{- include "rask.componentLabels" (list . "minio") | nindent 4 }}
type: Opaque
stringData:
  MINIO_ROOT_USER: {{ include "rask.minioAccessKey" . | quote }}
  MINIO_ROOT_PASSWORD: {{ include "rask.minioSecretKey" . | quote }}
{{- end }}
```

- [ ] **Step 4: Rewrite `chart/templates/configmap.yaml`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "rask.fullname" . }}-config
  labels:
    {{- include "rask.labels" . | nindent 4 }}
data:
  {{- range $k, $v := .Values.config }}
  {{ $k }}: {{ $v | quote }}
  {{- end }}
  # Gateway upstreams (in-cluster DNS).
  RASK_CORE_API_URL: {{ printf "http://%s-core-api:8801" (include "rask.fullname" .) | quote }}
  RASK_SEARCH_API_URL: {{ printf "http://%s-search-api:8802" (include "rask.fullname" .) | quote }}
  RASK_VOLUMES_API_URL: {{ printf "http://%s-volumes-api:8803" (include "rask.fullname" .) | quote }}
  RASK_RAY_API_URL: {{ printf "http://%s-ray-api:8804" (include "rask.fullname" .) | quote }}
  RASK_ORCH_API_URL: {{ printf "http://%s-orchestrator:8810" (include "rask.fullname" .) | quote }}
  {{- if .Values.ray.enabled }}
  RAY_DASHBOARD_URL: {{ printf "http://%s-ray-head-svc:%v" (include "rask.fullname" .) .Values.ray.dashboardPort | quote }}
  {{- end }}
```

- [ ] **Step 5: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/secrets.yaml | grep -E "name: rask-(app|postgres|minio)"
helm template rask ./chart --show-only templates/configmap.yaml | grep -E "RASK_CORE_API_URL|RAY_DASHBOARD_URL|RASK_API_PREFIX"
```
Expected: three secret names printed; the three config keys printed with `rask-core-api:8801`, `rask-ray-head-svc:8265`, and `/api`.

- [ ] **Step 6: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/secrets.yaml chart/templates/configmap.yaml
git commit -m "chart: generated app/postgres/minio secrets + fleet configmap"
```

---

## Task 3: In-cluster Postgres

**Files:**
- Create: `chart/templates/postgres.yaml`

**Interfaces:**
- Consumes: Secret `rask-postgres` (Task 2), `.Values.postgres`.
- Produces: StatefulSet `rask-postgres`, Service `rask-postgres:5432`. Other pods reach it at `rask-postgres:5432`.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/postgres.yaml 2>&1 | grep -q "kind: StatefulSet" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL` (template missing).

- [ ] **Step 3: Create `chart/templates/postgres.yaml`**

```yaml
{{- if .Values.postgres.enabled }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "rask.fullname" . }}-postgres
  labels:
    {{- include "rask.componentLabels" (list . "postgres") | nindent 4 }}
spec:
  serviceName: {{ include "rask.fullname" . }}-postgres
  replicas: 1
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: postgres
  template:
    metadata:
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: postgres
    spec:
      containers:
        - name: postgres
          image: {{ .Values.postgres.image | quote }}
          ports:
            - name: pg
              containerPort: {{ .Values.postgres.port }}
          envFrom:
            - secretRef:
                name: {{ include "rask.fullname" . }}-postgres
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          readinessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U {{ .Values.postgres.user }}"]
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["sh", "-c", "pg_isready -U {{ .Values.postgres.user }}"]
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            {{- toYaml .Values.postgres.resources | nindent 12 }}
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: {{ .Values.postgres.storageClass | quote }}
        resources:
          requests:
            storage: {{ .Values.postgres.storage | quote }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-postgres
  labels:
    {{- include "rask.componentLabels" (list . "postgres") | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - name: pg
      port: {{ .Values.postgres.port }}
      targetPort: pg
  selector:
    {{- include "rask.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: postgres
{{- end }}
```

- [ ] **Step 4: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/postgres.yaml | grep -E "kind: (StatefulSet|Service)|name: rask-postgres|storage: 8Gi"
helm template rask ./chart --set postgres.enabled=false --show-only templates/postgres.yaml 2>&1 | grep -q "kind:" && echo "FAIL: rendered while disabled" || echo "toggle OK"
```
Expected: StatefulSet + Service + `name: rask-postgres` + `storage: 8Gi`; toggle prints `toggle OK`.

- [ ] **Step 5: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/postgres.yaml
git commit -m "chart: in-cluster postgres statefulset + service"
```

---

## Task 4: In-cluster MinIO + buckets Job

**Files:**
- Create: `chart/templates/minio.yaml`

**Interfaces:**
- Consumes: Secret `rask-minio` (Task 2), `.Values.minio`.
- Produces: StatefulSet `rask-minio`, Service `rask-minio:9000` (+ console 9001), post-install Job `rask-minio-buckets`. Reached at `rask-minio:9000`.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/minio.yaml 2>&1 | grep -q "name: rask-minio-buckets" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL`.

- [ ] **Step 3: Create `chart/templates/minio.yaml`**

```yaml
{{- if .Values.minio.enabled }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "rask.fullname" . }}-minio
  labels:
    {{- include "rask.componentLabels" (list . "minio") | nindent 4 }}
spec:
  serviceName: {{ include "rask.fullname" . }}-minio
  replicas: 1
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: minio
  template:
    metadata:
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: minio
    spec:
      containers:
        - name: minio
          image: {{ .Values.minio.image | quote }}
          args: ["server", "/data", "--console-address", ":{{ .Values.minio.consolePort }}"]
          envFrom:
            - secretRef:
                name: {{ include "rask.fullname" . }}-minio
          ports:
            - name: s3
              containerPort: {{ .Values.minio.port }}
            - name: console
              containerPort: {{ .Values.minio.consolePort }}
          volumeMounts:
            - name: data
              mountPath: /data
          readinessProbe:
            httpGet: {path: /minio/health/ready, port: s3}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /minio/health/live, port: s3}
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            {{- toYaml .Values.minio.resources | nindent 12 }}
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: {{ .Values.minio.storageClass | quote }}
        resources:
          requests:
            storage: {{ .Values.minio.storage | quote }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-minio
  labels:
    {{- include "rask.componentLabels" (list . "minio") | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - name: s3
      port: {{ .Values.minio.port }}
      targetPort: s3
    - name: console
      port: {{ .Values.minio.consolePort }}
      targetPort: console
  selector:
    {{- include "rask.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: minio
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "rask.fullname" . }}-minio-buckets
  labels:
    {{- include "rask.componentLabels" (list . "minio-buckets") | nindent 4 }}
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 10
  template:
    metadata:
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: minio-buckets
    spec:
      restartPolicy: OnFailure
      containers:
        - name: mc
          image: {{ .Values.minio.mcImage | quote }}
          envFrom:
            - secretRef:
                name: {{ include "rask.fullname" . }}-minio
          command:
            - /bin/sh
            - -c
            - |
              set -e
              until mc alias set local http://{{ include "rask.fullname" . }}-minio:{{ .Values.minio.port }} "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; do
                echo "waiting for minio..."; sleep 3;
              done
              {{- range .Values.minio.buckets }}
              mc mb -p local/{{ . }} || true
              {{- end }}
              echo "buckets ready"
{{- end }}
```

- [ ] **Step 4: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/minio.yaml | grep -E "kind: (StatefulSet|Service|Job)|mc mb -p local/images-batch(-alto|-search)?"
```
Expected: StatefulSet, Service, Job present; three `mc mb -p local/images-batch*` lines.

- [ ] **Step 5: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/minio.yaml
git commit -m "chart: in-cluster minio statefulset + service + buckets job"
```

---

## Task 5: Fleet deployments + services

**Files:**
- Create: `chart/templates/fleet.yaml`

**Interfaces:**
- Consumes: `.Values.services`, `.Values.image`, `.Values.resources.fleet`, `.Values.orchestrator.autostart`, ConfigMap `rask-config`, app/`existingSecret` Secret.
- Produces: Deployment + Service per service key (`rask-core-api` … `rask-gateway`), each on its own port. Health probes hit `/api/health`. Init-containers wait on `waitFor` deps. Gateway is the ingress target.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/fleet.yaml 2>&1 | grep -c "kind: Deployment"
```

- [ ] **Step 2: Run to verify it fails**

Expected: helm error / `0` (template missing).

- [ ] **Step 3: Create `chart/templates/fleet.yaml`**

```yaml
{{- $root := . }}
{{- $secretName := .Values.existingSecret | default (printf "%s-app" (include "rask.fullname" .)) }}
{{- range $name, $svc := .Values.services }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rask.fullname" $root }}-{{ $name }}
  labels:
    {{- include "rask.componentLabels" (list $root $name) | nindent 4 }}
spec:
  replicas: {{ $svc.replicas | default 1 }}
  {{- if $svc.singleton }}
  strategy:
    type: Recreate
  {{- end }}
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" $root | nindent 6 }}
      app.kubernetes.io/component: {{ $name }}
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
      labels:
        {{- include "rask.selectorLabels" $root | nindent 8 }}
        app.kubernetes.io/component: {{ $name }}
    spec:
      {{- with $root.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "rask.serviceAccountName" $root }}
      {{- if $svc.waitFor }}
      initContainers:
        {{- range $dep := $svc.waitFor }}
        {{- if eq $dep "postgres" }}
        - name: wait-postgres
          image: busybox:1.36
          command: ["sh","-c","until nc -z {{ include "rask.fullname" $root }}-postgres {{ $root.Values.postgres.port }}; do echo waiting postgres; sleep 2; done"]
        {{- end }}
        {{- if eq $dep "minio" }}
        - name: wait-minio
          image: busybox:1.36
          command: ["sh","-c","until nc -z {{ include "rask.fullname" $root }}-minio {{ $root.Values.minio.port }}; do echo waiting minio; sleep 2; done"]
        {{- end }}
        {{- end }}
      {{- end }}
      containers:
        - name: {{ $name }}
          image: "{{ $root.Values.image.repository }}{{ if $root.Values.image.repository }}/{{ end }}{{ $name }}:{{ $root.Values.image.tag }}"
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command: ["uvicorn"]
          args:
            - {{ $svc.module | quote }}
            - "--host=0.0.0.0"
            - "--port={{ $svc.port }}"
            {{- range $a := ($svc.extraArgs | default list) }}
            - {{ $a | quote }}
            {{- end }}
          ports:
            - name: http
              containerPort: {{ $svc.port }}
          envFrom:
            - configMapRef:
                name: {{ include "rask.fullname" $root }}-config
            - secretRef:
                name: {{ $secretName }}
          env:
            - name: RASK_ORCHESTRATOR_AUTOSTART
              value: {{ if $svc.singleton }}{{ $root.Values.orchestrator.autostart | quote }}{{ else }}"false"{{ end }}
          readinessProbe:
            httpGet: {path: /api/health, port: http}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /api/health, port: http}
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            {{- toYaml $root.Values.resources.fleet | nindent 12 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" $root }}-{{ $name }}
  labels:
    {{- include "rask.componentLabels" (list $root $name) | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ $svc.port }}
      targetPort: http
  selector:
    {{- include "rask.selectorLabels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
---
{{- end }}
```

- [ ] **Step 4: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/fleet.yaml | grep -c "kind: Deployment"   # expect 6
helm template rask ./chart --show-only templates/fleet.yaml | grep -E "name: rask-(gateway|core-api|orchestrator)$"
helm template rask ./chart --show-only templates/fleet.yaml | grep -A1 "name: rask-orchestrator$" | grep -q "Recreate" || \
  helm template rask ./chart --show-only templates/fleet.yaml | grep -q "type: Recreate" && echo "orchestrator Recreate OK"
helm template rask ./chart --show-only templates/fleet.yaml | grep -- "--forwarded-allow-ips=127.0.0.1"
```
Expected: `6`; the three service names; `orchestrator Recreate OK`; gateway forwarded-allow-ips arg present.

- [ ] **Step 5: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/fleet.yaml
git commit -m "chart: fleet deployments + services (ranged over services map)"
```

---

## Task 6: Migration job retarget

**Files:**
- Modify: `chart/templates/migration-job.yaml`

**Interfaces:**
- Consumes: `.Values.image` (core-api), ConfigMap `rask-config`, app/`existingSecret` Secret, `.Values.migrations`.
- Produces: pre-install/pre-upgrade Job `rask-migrate` using the `core-api:dev` image with a `pg_isready` init-container.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/migration-job.yaml | grep -q "core-api:dev" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL` (still references `.Values.viewer.image`, which no longer exists — helm errors).

- [ ] **Step 3: Rewrite `chart/templates/migration-job.yaml`**

```yaml
{{- if .Values.migrations.enabled }}
{{- $secretName := .Values.existingSecret | default (printf "%s-app" (include "rask.fullname" .)) }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "rask.fullname" . }}-migrate
  labels:
    {{- include "rask.componentLabels" (list . "migrate") | nindent 4 }}
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "0"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 3
  template:
    metadata:
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: migrate
    spec:
      restartPolicy: Never
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "rask.serviceAccountName" . }}
      {{- if .Values.postgres.enabled }}
      initContainers:
        - name: wait-postgres
          image: busybox:1.36
          command: ["sh","-c","until nc -z {{ include "rask.fullname" . }}-postgres {{ .Values.postgres.port }}; do echo waiting postgres; sleep 2; done"]
      {{- end }}
      containers:
        - name: migrate
          image: "{{ .Values.image.repository }}{{ if .Values.image.repository }}/{{ end }}core-api:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command:
            {{- toYaml .Values.migrations.command | nindent 12 }}
          envFrom:
            - configMapRef:
                name: {{ include "rask.fullname" . }}-config
            - secretRef:
                name: {{ $secretName }}
          resources:
            {{- toYaml .Values.migrations.resources | nindent 12 }}
{{- end }}
```

> Note: migration is `post-install` (weight 0) so it runs after Postgres is scheduled; the `wait-postgres` init-container blocks until the DB accepts connections. The MinIO buckets Job is weight 5 (after migration).

- [ ] **Step 4: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/migration-job.yaml | grep -E "core-api:dev|wait-postgres|alembic upgrade head"
```
Expected: image `core-api:dev`, the `wait-postgres` init-container, and the alembic command.

- [ ] **Step 5: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/migration-job.yaml
git commit -m "chart: retarget migration job to core-api image + pg wait"
```

---

## Task 7: Frontend + ingress

**Files:**
- Modify: `chart/templates/frontend-deployment.yaml`
- Modify: `chart/templates/frontend-service.yaml`
- Modify: `chart/templates/ingress.yaml`

**Interfaces:**
- Consumes: `.Values.frontend` (port 3000, origin), gateway service `rask-gateway:8888`.
- Produces: Deployment + Service `rask-frontend:3000` (SSR, env `RASK_GATEWAY_URL`/`ORIGIN`); Ingress routing `/api`→gateway, `/`→frontend.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/ingress.yaml | grep -q "rask-gateway" && \
helm template rask ./chart --show-only templates/frontend-deployment.yaml | grep -q "RASK_GATEWAY_URL" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL` (ingress points at `rask-viewer`; frontend has no env / port 8080).

- [ ] **Step 3: Rewrite `chart/templates/frontend-deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rask.fullname" . }}-frontend
  labels:
    {{- include "rask.componentLabels" (list . "frontend") | nindent 4 }}
spec:
  replicas: {{ .Values.frontend.replicas }}
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: frontend
  template:
    metadata:
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: frontend
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "rask.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.frontend.podSecurityContext | nindent 8 }}
      containers:
        - name: frontend
          image: "{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag }}"
          imagePullPolicy: {{ .Values.frontend.image.pullPolicy }}
          securityContext:
            {{- toYaml .Values.frontend.securityContext | nindent 12 }}
          env:
            - name: RASK_GATEWAY_URL
              value: {{ printf "http://%s-gateway:8888" (include "rask.fullname" .) | quote }}
            - name: ORIGIN
              value: {{ .Values.frontend.origin | quote }}
            - name: PORT
              value: {{ .Values.frontend.service.port | quote }}
            - name: HOST
              value: "0.0.0.0"
          ports:
            - name: http
              containerPort: {{ .Values.frontend.service.port }}
          livenessProbe:
            httpGet: {path: /, port: http}
            initialDelaySeconds: 5
            periodSeconds: 20
          readinessProbe:
            httpGet: {path: /, port: http}
            initialDelaySeconds: 3
            periodSeconds: 10
          resources:
            {{- toYaml .Values.frontend.resources | nindent 12 }}
```

- [ ] **Step 4: Rewrite `chart/templates/frontend-service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-frontend
  labels:
    {{- include "rask.componentLabels" (list . "frontend") | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ .Values.frontend.service.port }}
      targetPort: http
  selector:
    {{- include "rask.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: frontend
```

- [ ] **Step 5: Rewrite `chart/templates/ingress.yaml`**

```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "rask.fullname" . }}
  labels:
    {{- include "rask.labels" . | nindent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  {{- with .Values.ingress.className }}
  ingressClassName: {{ . }}
  {{- end }}
  {{- with .Values.ingress.tls }}
  tls:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: {{ include "rask.fullname" . }}-gateway
                port:
                  number: 8888
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "rask.fullname" . }}-frontend
                port:
                  number: {{ .Values.frontend.service.port }}
{{- end }}
```

- [ ] **Step 6: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/ingress.yaml | grep -E "rask-gateway|rask-frontend|number: (8888|3000)"
helm template rask ./chart --show-only templates/frontend-deployment.yaml | grep -E "RASK_GATEWAY_URL|ORIGIN|containerPort: 3000"
```
Expected: ingress routes to `rask-gateway:8888` and `rask-frontend:3000`; frontend env + port 3000 present.

- [ ] **Step 7: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/frontend-deployment.yaml chart/templates/frontend-service.yaml chart/templates/ingress.yaml
git commit -m "chart: SSR frontend (port 3000 + gateway/origin env) and gateway/frontend ingress"
```

---

## Task 8: KubeRay RayService + NOTES

**Files:**
- Create: `chart/templates/rayservice.yaml`
- Modify: `chart/templates/NOTES.txt`

**Interfaces:**
- Consumes: `.Values.ray`, Secret (HF_TOKEN), `.Values.config` serve env.
- Produces: `RayService` CR named `rask-ray` (KubeRay creates `rask-ray-head-svc`). Serves `runner.htrflow_service:htrflow_app` at `/htrflow` on the GPU head.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
helm template rask ./chart --show-only templates/rayservice.yaml 2>&1 | grep -q "kind: RayService" && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL`.

- [ ] **Step 3: Create `chart/templates/rayservice.yaml`**

```yaml
{{- if .Values.ray.enabled }}
{{- $secretName := .Values.existingSecret | default (printf "%s-app" (include "rask.fullname" .)) }}
apiVersion: ray.io/v1
kind: RayService
metadata:
  name: {{ include "rask.fullname" . }}-ray
  labels:
    {{- include "rask.componentLabels" (list . "ray") | nindent 4 }}
spec:
  serveConfigV2: |
    applications:
      - name: htrflow
        import_path: {{ .Values.ray.importPath }}
        route_prefix: {{ .Values.ray.serveRoutePrefix }}
        runtime_env:
          env_vars:
            RASK_SERVE_REPLICAS: "{{ .Values.config.RASK_SERVE_REPLICAS }}"
            RASK_SERVE_GPU_FRAC: "{{ .Values.config.RASK_SERVE_GPU_FRAC }}"
            HF_HUB_DISABLE_IMPLICIT_TOKEN: "0"
  rayClusterConfig:
    rayVersion: "2.9.0"
    headGroupSpec:
      rayStartParams:
        dashboard-host: "0.0.0.0"
        num-gpus: "{{ .Values.ray.gpuCount }}"
      template:
        spec:
          runtimeClassName: {{ .Values.ray.runtimeClassName }}
          containers:
            - name: ray-head
              image: "{{ .Values.ray.image.repository }}:{{ .Values.ray.image.tag }}"
              imagePullPolicy: {{ .Values.ray.image.pullPolicy }}
              env:
                - name: RAY_ENABLE_UV_RUN_RUNTIME_ENV
                  value: "0"
                - name: HF_HOME
                  value: /cache/hf
                - name: HF_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: {{ $secretName }}
                      key: HF_TOKEN
              ports:
                - {containerPort: 6379, name: gcs}
                - {containerPort: 8265, name: dashboard}
                - {containerPort: 10001, name: client}
                - {containerPort: 8000, name: serve}
              resources:
                requests:
                  {{- toYaml .Values.ray.resources.requests | nindent 18 }}
                limits:
                  nvidia.com/gpu: {{ .Values.ray.gpuCount }}
                  {{- toYaml .Values.ray.resources.limits | nindent 18 }}
              volumeMounts:
                - {name: dshm, mountPath: /dev/shm}
                - {name: hf-cache, mountPath: /cache/hf}
          volumes:
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: {{ .Values.ray.shmSize }}
            - name: hf-cache
              persistentVolumeClaim:
                claimName: {{ include "rask.fullname" . }}-hf-cache
    workerGroupSpecs: []
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "rask.fullname" . }}-hf-cache
  labels:
    {{- include "rask.componentLabels" (list . "ray") | nindent 4 }}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: {{ .Values.ray.hfCacheStorage }}
{{- end }}
```

> **Verification risk (from spec):** confirm against the installed KubeRay version that (a) the head service is named `rask-ray-head-svc` — if not, adjust `RAY_DASHBOARD_URL` in `configmap.yaml`; (b) `import_path: runner.htrflow_service:htrflow_app` resolves under KubeRay's serve runtime (same import `deploy_serve.py` uses). Both are checked live in Task 11.

- [ ] **Step 4: Rewrite `chart/templates/NOTES.txt`**

```
rask {{ .Chart.AppVersion }} deployed as release "{{ .Release.Name }}".

Fleet services (ClusterIP):
{{- range $name, $svc := .Values.services }}
  - {{ $name }}: {{ include "rask.fullname" $ }}-{{ $name }}:{{ $svc.port }}
{{- end }}
  - frontend: {{ include "rask.fullname" . }}-frontend:{{ .Values.frontend.service.port }}
{{- if .Values.postgres.enabled }}
  - postgres: {{ include "rask.fullname" . }}-postgres:{{ .Values.postgres.port }} (in-cluster)
{{- end }}
{{- if .Values.minio.enabled }}
  - minio:    {{ include "rask.fullname" . }}-minio:{{ .Values.minio.port }} (in-cluster, console :{{ .Values.minio.consolePort }})
{{- end }}
{{- if .Values.ray.enabled }}
  - ray:      {{ include "rask.fullname" . }}-ray-head-svc:{{ .Values.ray.dashboardPort }} (KubeRay; htrflow at {{ .Values.ray.serveRoutePrefix }})
{{- end }}

{{- if .Values.ingress.enabled }}
UI:  http://{{ .Values.ingress.host }}/      API: http://{{ .Values.ingress.host }}/api/health
Add "127.0.0.1 {{ .Values.ingress.host }}" to /etc/hosts if it is not resolvable.
{{- else }}
Ingress disabled — port-forward the gateway:
  kubectl port-forward svc/{{ include "rask.fullname" . }}-gateway 8888
{{- end }}

Reminders:
  * The orchestrator starts only if RASK_ORCHESTRATOR_AUTOSTART=true (default false);
    otherwise POST /api/v1/orchestrator/start.
  * DO NOT scale the orchestrator above 1 replica (in-process singleton).
```

- [ ] **Step 5: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
helm lint chart/
helm template rask ./chart >/tmp/render.yaml 2>&1; echo "render exit=$?"
grep -E "kind: RayService|import_path: runner.htrflow_service:htrflow_app|nvidia.com/gpu" /tmp/render.yaml
grep -ci viewer /tmp/render.yaml   # expect 0
```
Expected: lint clean; render exit 0; RayService + import_path + GPU limit present; `0` viewer references.

- [ ] **Step 6: Commit**

```bash
cd /home/morgan/rask-main
git add chart/templates/rayservice.yaml chart/templates/NOTES.txt
git commit -m "chart: KubeRay RayService for htrflow + HF cache PVC + NOTES"
```

---

## Task 9: `make k3s-install` (k3s + helm + device-plugin + KubeRay)

**Files:**
- Create: `scripts/k3s-install.sh`
- Modify: `Makefile`

**Interfaces:**
- Produces: `make k3s-install` — idempotent host setup. Leaves a working k3s with kubectl/helm, `nvidia` RuntimeClass, the NVIDIA device-plugin advertising `nvidia.com/gpu`, and the KubeRay operator installed.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
test -x scripts/k3s-install.sh && grep -q "k3s-install:" Makefile && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL`.

- [ ] **Step 3: Create `scripts/k3s-install.sh`**

```bash
#!/usr/bin/env bash
# One-time host setup for the local rask k3s stack. Idempotent; needs sudo.
# Installs: k3s (bundled containerd + Traefik + kubectl) -> helm ->
# NVIDIA k8s device-plugin -> KubeRay operator.
set -euo pipefail

KUBERAY_VERSION="${KUBERAY_VERSION:-1.4.2}"
DEVICE_PLUGIN_VERSION="${DEVICE_PLUGIN_VERSION:-v0.17.4}"
KUBECONFIG_PATH="/etc/rancher/k3s/k3s.yaml"

echo ">> [1/4] k3s"
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | sh -
fi
sudo k3s kubectl get nodes

# Make kubectl/helm work without sudo for this user.
export KUBECONFIG="$KUBECONFIG_PATH"
sudo chmod 644 "$KUBECONFIG_PATH" || true

echo ">> [2/4] helm"
if ! command -v helm >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

echo ">> [3/4] NVIDIA device-plugin + runtimeclass"
# k3s auto-detects the nvidia container runtime when nvidia-container-toolkit is
# present on the host (nvidia-ctk is already installed here). Ensure the runtimeclass.
sudo k3s kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF
sudo k3s kubectl apply -f "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/${DEVICE_PLUGIN_VERSION}/deployments/static/nvidia-device-plugin.yml"

echo ">> [4/4] KubeRay operator"
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ 2>/dev/null || true
helm repo update kuberay
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "${KUBERAY_VERSION}" \
  --namespace kuberay-operator --create-namespace --wait

echo ">> waiting for GPU to be advertised on the node..."
for i in $(seq 1 30); do
  if sudo k3s kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' | grep -q '[1-9]'; then
    echo "GPU advertised."; break
  fi
  echo "  ...not yet ($i)"; sleep 5
done

echo "k3s-install done. Export KUBECONFIG=$KUBECONFIG_PATH for kubectl/helm."
```

- [ ] **Step 4: Make it executable and add the Makefile target**

```bash
cd /home/morgan/rask-main
chmod +x scripts/k3s-install.sh
```

Append to the `.PHONY` line additions and add this target near the `compose-*` block in `Makefile`:

```makefile
# ---- local k3s ------------------------------------------------------------
KUBECONFIG ?= /etc/rancher/k3s/k3s.yaml
HELM ?= KUBECONFIG=$(KUBECONFIG) helm
KUBECTL ?= KUBECONFIG=$(KUBECONFIG) kubectl
K3S_IMAGES = $(COMPOSE_IMAGES) frontend ray

k3s-install: ## One-time host setup: k3s + helm + NVIDIA device-plugin + KubeRay operator (sudo)
	./scripts/k3s-install.sh
```

Add `k3s-install k3s-build k3s-import k3s-up k3s-down k3s-purge` to the `.PHONY:` list at the top of the Makefile.

- [ ] **Step 5: Run to verify the target is wired (no live install in CI)**

Run:
```bash
cd /home/morgan/rask-main
make -n k3s-install
bash -n scripts/k3s-install.sh && echo "script syntax OK"
```
Expected: `make -n` prints `./scripts/k3s-install.sh`; `script syntax OK`.

- [ ] **Step 6: Commit**

```bash
cd /home/morgan/rask-main
git add scripts/k3s-install.sh Makefile
git commit -m "make: k3s-install (k3s + helm + nvidia device-plugin + kuberay operator)"
```

---

## Task 10: `make k3s-build / import / up / down / purge`

**Files:**
- Modify: `Makefile`

**Interfaces:**
- Consumes: `K3S_IMAGES`, `.docker/*.dockerfile`, `chart/`.
- Produces: `k3s-build` (buildx `:dev`), `k3s-import` (`k3s ctr images import`), `k3s-up` (`helm upgrade --install rask`), `k3s-down`, `k3s-purge`.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
grep -q "k3s-up:" Makefile && echo PASS || echo FAIL
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL`.

- [ ] **Step 3: Add the targets to `Makefile`** (below `k3s-install`)

```makefile
k3s-build: ## Build all fleet + frontend + ray images as :dev (native arm64)
	@for s in $(COMPOSE_IMAGES); do \
	  echo ">> building $$s:dev"; \
	  docker buildx build -f .docker/$$s.dockerfile -t $$s:dev --load . || exit 1; \
	done
	docker buildx build -f .docker/frontend.dockerfile -t frontend:dev --load .
	docker buildx build -f .docker/ray.dockerfile -t ray:dev --load .

k3s-import: ## Side-load :dev images into k3s containerd
	@for s in $(K3S_IMAGES); do \
	  echo ">> importing $$s:dev"; \
	  docker save $$s:dev | sudo k3s ctr images import - || exit 1; \
	done

k3s-up: ## Install/upgrade the rask release and wait for the gateway
	$(HELM) upgrade --install rask ./chart --wait --timeout 10m
	$(KUBECTL) rollout status deploy/rask-gateway --timeout=300s
	@echo "UI → http://rask.local/   (add '127.0.0.1 rask.local' to /etc/hosts)"
	@echo "API → http://rask.local/api/health"

k3s-down: ## Uninstall the rask release (keep PVCs)
	$(HELM) uninstall rask || true

k3s-purge: k3s-down ## Uninstall + delete PVCs (postgres/minio/hf-cache data)
	$(KUBECTL) delete pvc -l app.kubernetes.io/instance=rask || true
```

- [ ] **Step 4: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
for t in k3s-build k3s-import k3s-up k3s-down k3s-purge; do make -n $t >/dev/null && echo "$t OK"; done
```
Expected: all five print `OK` (dry-run resolves without error).

- [ ] **Step 5: Commit**

```bash
cd /home/morgan/rask-main
git add Makefile
git commit -m "make: k3s build/import/up/down/purge targets"
```

---

## Task 11: Live bring-up + end-to-end verification

**Files:** none (verification task; small fixups to `chart/` only if checks fail).

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a verified running stack and any required corrections (e.g. RAY head svc name, import path).

- [ ] **Step 1: Install the cluster prerequisites**

Run:
```bash
cd /home/morgan/rask-main
make k3s-install
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'; echo
```
Expected: node Ready; GPU allocatable `1`.

- [ ] **Step 2: Build + import images**

Run: `make k3s-build && make k3s-import`
Expected: every `<img>:dev` reports `unpacking ... done` on import.

- [ ] **Step 3: Set the HF token and deploy**

Run:
```bash
cd /home/morgan/rask-main
HF=$(grep -E '^HF_TOKEN=' .env | cut -d= -f2-)
KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm upgrade --install rask ./chart \
  --set secrets.hfToken="$HF" --wait --timeout 15m
```
Expected: `STATUS: deployed`. If it times out, continue to Step 4 to diagnose.

- [ ] **Step 4: Verify pods, jobs, and the RayService**

Run:
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get pods
kubectl get jobs
kubectl get svc | grep ray         # confirm the head service name
kubectl get rayservice rask-ray -o jsonpath='{.status.serviceStatus}'; echo
```
Expected: all fleet + postgres + minio + ray-head pods `Running`/`Ready`; `rask-migrate` and `rask-minio-buckets` Jobs `Complete`; a ray head service exists.
**If** the head service is NOT `rask-ray-head-svc`, edit `chart/templates/configmap.yaml` `RAY_DASHBOARD_URL` (and `NOTES.txt`) to the actual name, then `git commit -m "chart: correct ray head service name"` and re-run `make k3s-up`.

- [ ] **Step 5: Verify the htrflow Serve app is healthy**

Run:
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl exec deploy/rask-ray-head 2>/dev/null -- serve status || \
  kubectl logs -l ray.io/node-type=head --tail=50
```
Expected: Serve app `htrflow` reports `RUNNING`.
**If** the import fails (`ModuleNotFoundError` for `runner.htrflow_service`), the RayService `runtime_env` needs the image's interpreter — add `runtime_env.py_executable` or a `working_dir`/`pip` block mirroring `deploy_serve.py`'s `_connect()`; fix in `chart/templates/rayservice.yaml`, commit, re-run `make k3s-up`.

- [ ] **Step 6: End-to-end smoke (upload → register → HTR → ALTO)**

Run:
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
grep -q "rask.local" /etc/hosts || echo "127.0.0.1 rask.local" | sudo tee -a /etc/hosts
# expose ingress on localhost:80 (Traefik) — k3s serves it on the node IP:80
curl -fsS http://rask.local/api/health && echo " <- gateway healthy"
# upload a test image into MinIO under a volume prefix, then register + run:
kubectl port-forward svc/rask-minio 9000:9000 >/tmp/pf.log 2>&1 &
PF=$!; sleep 3
AKID=$(kubectl get secret rask-minio -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)
SKEY=$(kubectl get secret rask-minio -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)
AWS_ACCESS_KEY_ID=$AKID AWS_SECRET_ACCESS_KEY=$SKEY \
  aws --endpoint-url http://127.0.0.1:9000 s3 cp <a-test.jpg> s3://images-batch/testvol/page1.jpg
kill $PF
curl -fsS -X POST http://rask.local/api/v1/batches/testvol/register && echo " <- registered"
curl -fsS -X POST http://rask.local/api/v1/orchestrator/start && echo " <- orchestrator started"
```
Expected: gateway healthy; register returns a Batch JSON; orchestrator starts.

- [ ] **Step 7: Confirm ALTO output lands**

Run:
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl port-forward svc/rask-minio 9000:9000 >/tmp/pf.log 2>&1 &
PF=$!; sleep 3
AKID=$(kubectl get secret rask-minio -o jsonpath='{.data.MINIO_ROOT_USER}' | base64 -d)
SKEY=$(kubectl get secret rask-minio -o jsonpath='{.data.MINIO_ROOT_PASSWORD}' | base64 -d)
AWS_ACCESS_KEY_ID=$AKID AWS_SECRET_ACCESS_KEY=$SKEY \
  aws --endpoint-url http://127.0.0.1:9000 s3 ls s3://images-batch-alto/testvol/ --recursive
kill $PF
```
Expected: an ALTO `.xml` under `images-batch-alto/testvol/` (may take a few minutes; re-run until present). Open `http://rask.local/` and confirm the volume renders.

- [ ] **Step 8: Commit any fixups**

```bash
cd /home/morgan/rask-main
git add -A chart/
git commit -m "chart: live-verified local k3s bring-up fixups" || echo "no fixups needed"
```

---

## Task 12: Remove docker-compose + update docs

**Files:**
- Delete: `docker-compose.yml`, `.docker/ingress.Caddyfile`, `.docker/smoke-compose.sh`
- Modify: `Makefile` (remove `compose-*` targets + `compose-env` dependency)
- Modify: `README.md`, `CLAUDE.md`, `docs/architecture/deployment.md`
- Modify: `chart/README.md`

**Interfaces:**
- Produces: a repo whose only local-deploy path is k3s.

- [ ] **Step 1: Write the failing test**

```bash
cd /home/morgan/rask-main
test ! -f docker-compose.yml && ! grep -q "compose-up:" Makefile && echo PASS || echo "FAIL (compose still present)"
```

- [ ] **Step 2: Run to verify it fails**

Expected: `FAIL` (compose still present).

- [ ] **Step 3: Delete compose files**

```bash
cd /home/morgan/rask-main
git rm docker-compose.yml .docker/ingress.Caddyfile .docker/smoke-compose.sh
```

- [ ] **Step 4: Remove `compose-*` from the Makefile**

Delete the `compose-env`, `compose-build`, `compose-up`, `compose-down`, `compose-purge`, `compose-logs` target blocks and the `DC ?=`/`COMPOSE_IMAGES` line **only if `COMPOSE_IMAGES` is now unused** — it is still referenced by `k3s-build`/`k3s-import`, so **keep `COMPOSE_IMAGES`** and remove only `DC ?= docker compose` and the compose targets. Remove `compose-*` names from `.PHONY`.

Verify after editing:
```bash
cd /home/morgan/rask-main
grep -nE "compose-(env|build|up|down|purge|logs):" Makefile || echo "compose targets gone"
grep -q "COMPOSE_IMAGES" Makefile && echo "COMPOSE_IMAGES kept (good)"
```
Expected: `compose targets gone`; `COMPOSE_IMAGES kept (good)`.

- [ ] **Step 5: Update docs**

In `README.md` and `docs/architecture/deployment.md`: replace the docker-compose quickstart with the k3s flow:

```markdown
## Local deploy (k3s)

```bash
make k3s-install      # one-time: k3s + helm + NVIDIA device-plugin + KubeRay (sudo)
make k3s-build        # build fleet + frontend + ray images as :dev
make k3s-import       # side-load images into k3s
make k3s-up           # helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```
```

In `CLAUDE.md`: replace any `make compose-*` references with the `make k3s-*` equivalents and note the chart is the single local+prod deploy artifact (in-cluster deps gated by `postgres.enabled`/`minio.enabled`/`ray.enabled`).

In `chart/README.md`: rewrite to describe the fleet + in-cluster deps + toggles (supersedes the old viewer-only text).

- [ ] **Step 6: Run to verify it passes**

Run:
```bash
cd /home/morgan/rask-main
test ! -f docker-compose.yml && echo "compose removed"
grep -rIl "docker-compose\|compose-up" README.md CLAUDE.md docs/architecture/deployment.md && echo "FAIL: stale refs" || echo "docs clean"
helm lint chart/ && helm template rask ./chart >/dev/null && echo "chart still renders"
```
Expected: `compose removed`; `docs clean`; `chart still renders`.

- [ ] **Step 7: Commit**

```bash
cd /home/morgan/rask-main
git add -A
git commit -m "chore: remove docker-compose; document local k3s as the deploy path"
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- In-cluster Postgres/MinIO/Ray → Tasks 3, 4, 8. ✓
- Extend existing chart with `*.enabled` toggles (dual-purpose prod) → Tasks 1–8 (toggles in values; `existingSecret` override preserved). ✓
- KubeRay operator + RayService CRD → Tasks 8, 9. ✓
- `ctr import` image delivery → Task 10. ✓
- Traefik Ingress (`/`→frontend, `/api`→gateway) → Task 7. ✓
- `make k3s-install/build/import/up/down/purge` → Tasks 9, 10. ✓
- Remove docker-compose + docs → Task 12. ✓
- Single GPU htrflow, source/pipeline env → values `config` + RayService (Tasks 1, 8). ✓
- Verification (pods/jobs/GPU/RayService + end-to-end upload→register→ALTO) → Task 11. ✓

**Placeholder scan:** The only intentional fill-in is `<a-test.jpg>` in Task 11 Step 6 (operator supplies a real sample image) and `RAY head svc name` / `import_path` confirmations, which are explicit live-verification branches with named fixes — not deferred work.

**Type/name consistency:** Service names `rask-<component>` consistent across configmap upstreams (Task 2), fleet (Task 5), ingress (Task 7), RayService dashboard URL (Tasks 2, 8). API prefix `/api` + health `/api/health` consistent (values, fleet probes, ingress, verification). Frontend port `3000` consistent (values, deployment, service, ingress). Secret name resolution `existingSecret | default rask-app` identical in fleet, migration, rayservice.
