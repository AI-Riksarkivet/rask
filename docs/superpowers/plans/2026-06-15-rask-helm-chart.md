# rask Helm Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a plain Helm chart at `chart/` that deploys the rask application services (viewer + frontend + Alembic migration job) to Kubernetes, referencing external Postgres/S3/KubeRay via config and an existing Secret.

**Architecture:** A single, deploy-tool-agnostic Helm chart. The viewer is a singleton (`replicas: 1`, `strategy: Recreate`) because its in-process orchestrator must not run concurrently. The frontend scales freely. Sensitive config comes from an operator-created Secret (`existingSecret`); non-sensitive config from a chart-rendered ConfigMap. A pre-install/pre-upgrade hook Job runs `alembic upgrade head`. A single Ingress splits `/api` → viewer:8888 and `/` → frontend:8080.

**Tech Stack:** Helm 3 (v3.16 confirmed on PATH), Kubernetes, the existing `rask-viewer` / `rask-frontend` container images.

---

## Reference facts (verified against the repo)

- Viewer image: `EXPOSE 8888`, `CMD ["uvicorn","viewer.app:app", … "--forwarded-allow-ips","127.0.0.1"]`. Source copied to `/app` (so alembic lives at `/app/components/services/viewer/`).
- Frontend image: nginx-unprivileged, `USER 101`, `EXPOSE 8080`, serves SPA only (no `/api` proxy).
- Health endpoint: `GET /api/v1/health` (api_prefix default `/api/v1`, env `RASK_API_PREFIX`).
- Viewer env aliases (from `components/services/viewer/src/viewer/core/config.py`):
  - Secret: `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `HCP_ENDPOINT`, `HF_TOKEN`.
  - ConfigMap: `RASK_ORCHESTRATOR_AUTOSTART`, `RASK_ORCHESTRATOR_INTERVAL_SECONDS`, `RASK_ORCHESTRATOR_RECONCILE_SECONDS`, `RAY_DASHBOARD_URL` (Ray job submission — NOT a `ray://` address), `RASK_CACHE_BUCKET`, `RASK_OUTPUT_BUCKET`, `RASK_SEARCH_BUCKET`, `RASK_IIIF_URL`, `AWS_REGION`, `HCP_INSECURE`, `RASK_CORS_ORIGINS`.
- Migration command in dev: `DATABASE_URL=… uv run --package viewer alembic upgrade head`. In the runtime image the venv (with `alembic`) is on PATH, so the job runs `alembic upgrade head` from the alembic dir; fallback `python -m alembic upgrade head`.

## File structure

```
chart/
  Chart.yaml
  values.yaml
  .helmignore
  templates/
    _helpers.tpl
    serviceaccount.yaml
    configmap.yaml
    viewer-deployment.yaml
    viewer-service.yaml
    frontend-deployment.yaml
    frontend-service.yaml
    migration-job.yaml
    ingress.yaml
    NOTES.txt
```

Validation throughout uses `helm lint chart/` and `helm template rask chart/ …` piped to `grep`. There is no cluster in this environment; rendering is the test.

---

### Task 1: Scaffold the chart (Chart.yaml, values.yaml, helpers, .helmignore)

**Files:**
- Delete: `chart/.gitkeep`
- Create: `chart/Chart.yaml`, `chart/.helmignore`, `chart/values.yaml`, `chart/templates/_helpers.tpl`

- [ ] **Step 1: Write a render smoke-test (expected to fail — no chart yet)**

Run: `helm lint chart/`
Expected: FAIL — `Error: ... no Chart.yaml exists` (chart/ only has `.gitkeep`).

- [ ] **Step 2: Create `chart/Chart.yaml`**

```yaml
apiVersion: v2
name: rask
description: rask application services (viewer + frontend) for the Swedish National Archives HTR pipeline
type: application
version: 0.1.0
appVersion: "0.1.0"
home: https://github.com/AI-Riksarkivet/rask
maintainers:
  - name: Riksarkivet
```

- [ ] **Step 3: Create `chart/.helmignore`**

```
.git/
*.md
.DS_Store
*.tmp
ci/
```

- [ ] **Step 4: Create `chart/templates/_helpers.tpl`**

```yaml
{{- define "rask.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rask.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "rask.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "rask.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "rask.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "rask.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rask.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rask.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "rask.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
```

- [ ] **Step 5: Create `chart/values.yaml`**

```yaml
# -- Global image pull secrets (names of existing Secrets of type kubernetes.io/dockerconfigjson)
imagePullSecrets: []
nameOverride: ""
fullnameOverride: ""

serviceAccount:
  create: true
  name: ""
  annotations: {}

# Operator-created Secret holding DATABASE_URL, AWS_*, HCP_ENDPOINT, HF_TOKEN.
# REQUIRED. Nothing sensitive is rendered by this chart.
existingSecret: ""

# Non-sensitive env, rendered into a ConfigMap and shared by viewer + migrations.
config:
  RASK_ORCHESTRATOR_AUTOSTART: "false"
  RASK_ORCHESTRATOR_INTERVAL_SECONDS: "60"
  RASK_ORCHESTRATOR_RECONCILE_SECONDS: "600"
  RAY_DASHBOARD_URL: "http://rask-ray-head:8265"
  RASK_CACHE_BUCKET: "images-batch"
  RASK_OUTPUT_BUCKET: "images-batch-alto"
  RASK_SEARCH_BUCKET: "images-batch-search"
  RASK_IIIF_URL: "https://iiifintern-ai.ra.se"
  AWS_REGION: "us-east-1"
  HCP_INSECURE: "false"

viewer:
  replicas: 1   # MUST stay 1 — the in-process orchestrator is a singleton.
  image:
    repository: rask-viewer
    tag: ""        # defaults to .Chart.AppVersion when empty
    pullPolicy: IfNotPresent
  # CIDR of the ingress controller pods; passed to uvicorn --forwarded-allow-ips. Never "*".
  forwardedAllowIps: "127.0.0.1"
  service:
    port: 8888
  resources:
    requests: {cpu: "250m", memory: "512Mi"}
    limits: {cpu: "2", memory: "2Gi"}
  nodeSelector: {}
  tolerations: []
  affinity: {}
  podSecurityContext:
    runAsNonRoot: true
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: false
    capabilities: {drop: ["ALL"]}

frontend:
  replicas: 2
  image:
    repository: rask-frontend
    tag: ""
    pullPolicy: IfNotPresent
  service:
    port: 8080
  resources:
    requests: {cpu: "50m", memory: "64Mi"}
    limits: {cpu: "500m", memory: "256Mi"}
  nodeSelector: {}
  tolerations: []
  affinity: {}
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 101
  securityContext:
    allowPrivilegeEscalation: false
    capabilities: {drop: ["ALL"]}

migrations:
  enabled: true
  # Command run from the alembic directory inside the viewer image.
  command: ["sh", "-c", "cd /app/components/services/viewer && alembic upgrade head"]
  resources:
    requests: {cpu: "100m", memory: "256Mi"}
    limits: {cpu: "1", memory: "1Gi"}

ingress:
  enabled: true
  className: "nginx"
  host: rask.local
  annotations: {}
  tls: []   # e.g. [{secretName: rask-tls, hosts: [rask.example.org]}]
```

- [ ] **Step 6: Run lint (expected to pass with no templates yet besides helpers)**

Run: `helm lint chart/`
Expected: PASS — `1 chart(s) linted, 0 chart(s) failed` (an `[INFO]` about no icon is fine).

- [ ] **Step 7: Commit**

```bash
git rm chart/.gitkeep
git add chart/Chart.yaml chart/.helmignore chart/values.yaml chart/templates/_helpers.tpl
git commit -m "feat(chart): scaffold rask Helm chart"
```

---

### Task 2: ServiceAccount + ConfigMap

**Files:**
- Create: `chart/templates/serviceaccount.yaml`, `chart/templates/configmap.yaml`

- [ ] **Step 1: Render-test (expected to fail — no ConfigMap yet)**

Run: `helm template rask chart/ -s templates/configmap.yaml`
Expected: FAIL — `Error: could not find template templates/configmap.yaml in chart`.

- [ ] **Step 2: Create `chart/templates/serviceaccount.yaml`**

```yaml
{{- if .Values.serviceAccount.create -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "rask.serviceAccountName" . }}
  labels:
    {{- include "rask.labels" . | nindent 4 }}
  {{- with .Values.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 3: Create `chart/templates/configmap.yaml`**

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
  RASK_API_PREFIX: "/api/v1"
```

- [ ] **Step 4: Render-test (expected to pass)**

Run: `helm template rask chart/ -s templates/configmap.yaml | grep RASK_ORCHESTRATOR_AUTOSTART`
Expected: prints `RASK_ORCHESTRATOR_AUTOSTART: "false"`.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/serviceaccount.yaml chart/templates/configmap.yaml
git commit -m "feat(chart): add serviceaccount and config configmap"
```

---

### Task 3: Viewer Deployment + Service

**Files:**
- Create: `chart/templates/viewer-deployment.yaml`, `chart/templates/viewer-service.yaml`

- [ ] **Step 1: Render-test (expected to fail — no viewer template yet)**

Run: `helm template rask chart/ --set existingSecret=rask-secrets -s templates/viewer-deployment.yaml`
Expected: FAIL — `could not find template templates/viewer-deployment.yaml in chart`.

- [ ] **Step 2: Create `chart/templates/viewer-deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rask.fullname" . }}-viewer
  labels:
    {{- include "rask.labels" . | nindent 4 }}
    app.kubernetes.io/component: viewer
spec:
  replicas: {{ .Values.viewer.replicas }}
  strategy:
    type: Recreate   # singleton orchestrator: never overlap two viewers
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: viewer
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: viewer
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "rask.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.viewer.podSecurityContext | nindent 8 }}
      containers:
        - name: viewer
          image: "{{ .Values.viewer.image.repository }}:{{ .Values.viewer.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.viewer.image.pullPolicy }}
          securityContext:
            {{- toYaml .Values.viewer.securityContext | nindent 12 }}
          command: ["uvicorn"]
          args:
            - "viewer.app:app"
            - "--host=0.0.0.0"
            - "--port={{ .Values.viewer.service.port }}"
            - "--proxy-headers"
            - "--forwarded-allow-ips={{ .Values.viewer.forwardedAllowIps }}"
          ports:
            - name: http
              containerPort: {{ .Values.viewer.service.port }}
          envFrom:
            - configMapRef:
                name: {{ include "rask.fullname" . }}-config
            - secretRef:
                name: {{ required "existingSecret is required (DATABASE_URL, AWS_*, HF_TOKEN)" .Values.existingSecret }}
          livenessProbe:
            httpGet: {path: /api/v1/health, port: http}
            initialDelaySeconds: 15
            periodSeconds: 20
          readinessProbe:
            httpGet: {path: /api/v1/health, port: http}
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            {{- toYaml .Values.viewer.resources | nindent 12 }}
      {{- with .Values.viewer.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.viewer.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.viewer.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
```

- [ ] **Step 3: Create `chart/templates/viewer-service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-viewer
  labels:
    {{- include "rask.labels" . | nindent 4 }}
    app.kubernetes.io/component: viewer
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ .Values.viewer.service.port }}
      targetPort: http
  selector:
    {{- include "rask.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: viewer
```

- [ ] **Step 4: Render-test — singleton + required secret**

Run: `helm template rask chart/ --set existingSecret=rask-secrets -s templates/viewer-deployment.yaml | grep -E "replicas:|type: Recreate|forwarded-allow-ips|secretRef" `
Expected: shows `replicas: 1`, `type: Recreate`, the `--forwarded-allow-ips=127.0.0.1` arg, and `secretRef`.

- [ ] **Step 5: Render-test — missing secret fails loudly**

Run: `helm template rask chart/ -s templates/viewer-deployment.yaml`
Expected: FAIL — `Error: ... existingSecret is required (DATABASE_URL, AWS_*, HF_TOKEN)`.

- [ ] **Step 6: Commit**

```bash
git add chart/templates/viewer-deployment.yaml chart/templates/viewer-service.yaml
git commit -m "feat(chart): add viewer deployment (singleton) and service"
```

---

### Task 4: Frontend Deployment + Service

**Files:**
- Create: `chart/templates/frontend-deployment.yaml`, `chart/templates/frontend-service.yaml`

- [ ] **Step 1: Render-test (expected to fail)**

Run: `helm template rask chart/ --set existingSecret=x -s templates/frontend-deployment.yaml`
Expected: FAIL — `could not find template templates/frontend-deployment.yaml in chart`.

- [ ] **Step 2: Create `chart/templates/frontend-deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rask.fullname" . }}-frontend
  labels:
    {{- include "rask.labels" . | nindent 4 }}
    app.kubernetes.io/component: frontend
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
          image: "{{ .Values.frontend.image.repository }}:{{ .Values.frontend.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.frontend.image.pullPolicy }}
          securityContext:
            {{- toYaml .Values.frontend.securityContext | nindent 12 }}
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
      {{- with .Values.frontend.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.frontend.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.frontend.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
```

- [ ] **Step 3: Create `chart/templates/frontend-service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-frontend
  labels:
    {{- include "rask.labels" . | nindent 4 }}
    app.kubernetes.io/component: frontend
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

- [ ] **Step 4: Render-test (expected to pass)**

Run: `helm template rask chart/ --set existingSecret=x -s templates/frontend-deployment.yaml | grep -E "replicas:|runAsUser:"`
Expected: shows `replicas: 2` and `runAsUser: 101`.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/frontend-deployment.yaml chart/templates/frontend-service.yaml
git commit -m "feat(chart): add frontend deployment and service"
```

---

### Task 5: Alembic migration Job (helm hook)

**Files:**
- Create: `chart/templates/migration-job.yaml`

- [ ] **Step 1: Render-test (expected to fail)**

Run: `helm template rask chart/ --set existingSecret=x -s templates/migration-job.yaml`
Expected: FAIL — `could not find template templates/migration-job.yaml in chart`.

- [ ] **Step 2: Create `chart/templates/migration-job.yaml`**

```yaml
{{- if .Values.migrations.enabled }}
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "rask.fullname" . }}-migrate
  labels:
    {{- include "rask.labels" . | nindent 4 }}
    app.kubernetes.io/component: migrate
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  backoffLimit: 1
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
      securityContext:
        {{- toYaml .Values.viewer.podSecurityContext | nindent 8 }}
      containers:
        - name: migrate
          image: "{{ .Values.viewer.image.repository }}:{{ .Values.viewer.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.viewer.image.pullPolicy }}
          securityContext:
            {{- toYaml .Values.viewer.securityContext | nindent 12 }}
          command:
            {{- toYaml .Values.migrations.command | nindent 12 }}
          envFrom:
            - configMapRef:
                name: {{ include "rask.fullname" . }}-config
            - secretRef:
                name: {{ required "existingSecret is required for migrations (DATABASE_URL)" .Values.existingSecret }}
          resources:
            {{- toYaml .Values.migrations.resources | nindent 12 }}
{{- end }}
```

- [ ] **Step 3: Render-test — hook annotations present**

Run: `helm template rask chart/ --set existingSecret=x -s templates/migration-job.yaml | grep -E "helm.sh/hook:|alembic upgrade head|kind: Job"`
Expected: shows `kind: Job`, `helm.sh/hook: pre-install,pre-upgrade`, and the `alembic upgrade head` command.

- [ ] **Step 4: Render-test — disabling removes the Job**

Run: `helm template rask chart/ --set existingSecret=x --set migrations.enabled=false -s templates/migration-job.yaml`
Expected: FAIL/empty — `Error: could not find template ...` OR no output (the `if` guard yields nothing). Either confirms the Job is gone.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/migration-job.yaml
git commit -m "feat(chart): add alembic migration hook job"
```

---

### Task 6: Ingress

**Files:**
- Create: `chart/templates/ingress.yaml`

- [ ] **Step 1: Render-test (expected to fail)**

Run: `helm template rask chart/ --set existingSecret=x -s templates/ingress.yaml`
Expected: FAIL — `could not find template templates/ingress.yaml in chart`.

- [ ] **Step 2: Create `chart/templates/ingress.yaml`**

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
                name: {{ include "rask.fullname" . }}-viewer
                port:
                  number: {{ .Values.viewer.service.port }}
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "rask.fullname" . }}-frontend
                port:
                  number: {{ .Values.frontend.service.port }}
{{- end }}
```

- [ ] **Step 3: Render-test — both backends present, /api first**

Run: `helm template rask chart/ --set existingSecret=x -s templates/ingress.yaml | grep -E "ingressClassName:|path:|-viewer|-frontend"`
Expected: shows `ingressClassName: nginx`, `path: /api` mapped to `…-viewer`, and `path: /` mapped to `…-frontend`.

- [ ] **Step 4: Render-test — TLS toggles on**

Run: `helm template rask chart/ --set existingSecret=x --set "ingress.tls[0].secretName=rask-tls" --set "ingress.tls[0].hosts[0]=rask.example.org" -s templates/ingress.yaml | grep -A2 "tls:"`
Expected: shows the `tls:` block with `secretName: rask-tls`.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/ingress.yaml
git commit -m "feat(chart): add ingress splitting /api and / "
```

---

### Task 7: NOTES.txt + full-chart validation

**Files:**
- Create: `chart/templates/NOTES.txt`

- [ ] **Step 1: Create `chart/templates/NOTES.txt`**

```
rask {{ .Chart.AppVersion }} deployed as release "{{ .Release.Name }}".

Components:
  - viewer   (singleton, replicas={{ .Values.viewer.replicas }})  Service: {{ include "rask.fullname" . }}-viewer:{{ .Values.viewer.service.port }}
  - frontend (replicas={{ .Values.frontend.replicas }})           Service: {{ include "rask.fullname" . }}-frontend:{{ .Values.frontend.service.port }}

{{- if .Values.ingress.enabled }}
Ingress host: http://{{ .Values.ingress.host }}/   (UI)   and   /api/v1/health  (backend)
{{- else }}
Ingress disabled — reach the viewer via: kubectl port-forward svc/{{ include "rask.fullname" . }}-viewer {{ .Values.viewer.service.port }}
{{- end }}

Reminders:
  * existingSecret "{{ .Values.existingSecret }}" must hold DATABASE_URL, AWS_*, HCP_ENDPOINT, HF_TOKEN.
  * The orchestrator starts only if RASK_ORCHESTRATOR_AUTOSTART=true; otherwise POST /api/v1/orchestrator/start.
  * DO NOT scale the viewer above 1 replica (the orchestrator is a singleton).
```

- [ ] **Step 2: Full lint**

Run: `helm lint chart/ --set existingSecret=rask-secrets`
Expected: PASS — `1 chart(s) linted, 0 chart(s) failed`.

- [ ] **Step 3: Full render of every manifest**

Run: `helm template rask chart/ --set existingSecret=rask-secrets | grep -cE "^kind:"`
Expected: a count of `7` (ServiceAccount, ConfigMap, 2 Services, 2 Deployments, Ingress) — the migration Job is a hook and is excluded from default `helm template` output unless `--include-crds`/hooks shown; if your helm version prints hooks, expect `8`. Either is acceptable; confirm no render errors.

- [ ] **Step 4: Render with ingress disabled (sanity)**

Run: `helm template rask chart/ --set existingSecret=x --set ingress.enabled=false | grep -c "kind: Ingress"`
Expected: `0`.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/NOTES.txt
git commit -m "feat(chart): add NOTES.txt and finalize chart"
```

---

### Task 8: Update docs to reflect the chart exists

**Files:**
- Modify: `docs/architecture/deployment.md` (lines 1-6 and 39-44)
- Modify: `CLAUDE.md:85` (the "No … Helm" sentence)
- Create: `chart/README.md`

- [ ] **Step 1: Rewrite the top of `docs/architecture/deployment.md`**

Replace lines 1-6:

```markdown
# Deployment

rask ships a **Helm chart at `chart/`** that deploys the application services
(viewer + frontend + an Alembic migration job) to Kubernetes. Postgres, S3/MinIO,
and the KubeRay cluster are **external dependencies** referenced via config and an
operator-created Secret — the chart does not provision them. The `Makefile`
remains the runbook for local/dev operation.
```

- [ ] **Step 2: Replace the "Remote KubeRay" placeholder note in `docs/architecture/deployment.md`**

Replace lines 39-44 (the `## Remote KubeRay` block) with:

```markdown
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
```

- [ ] **Step 3: Update `CLAUDE.md:85`**

Find the sentence `**No Redis, no queue, no event bus, no docker-compose, no Helm.** The `Makefile` is the only runbook.` and replace with:

```markdown
**No Redis, no queue, no event bus, no docker-compose.** A Helm chart in `chart/` deploys the app services to Kubernetes (see `docs/architecture/deployment.md`); the `Makefile` is the local/dev runbook.
```

- [ ] **Step 4: Create `chart/README.md`**

```markdown
# rask Helm chart

Deploys the rask application services — **viewer** (FastAPI, singleton) and
**frontend** (SPA) — plus an Alembic migration hook. Postgres, S3/MinIO and the
KubeRay cluster are external; this chart only references them.

## Prerequisites

1. Images `rask-viewer` and `rask-frontend` pushed to a registry your cluster can
   pull (no CI builds these yet — build from `.docker/*.dockerfile`).
2. A Secret with: `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `HCP_ENDPOINT`, `HF_TOKEN`.

   ```bash
   kubectl create secret generic rask-secrets \
     --from-literal=DATABASE_URL='postgresql+asyncpg://…' \
     --from-literal=AWS_ACCESS_KEY_ID=… \
     --from-literal=AWS_SECRET_ACCESS_KEY=… \
     --from-literal=HCP_ENDPOINT=… \
     --from-literal=HF_TOKEN=…
   ```

## Install

```bash
helm install rask chart/ \
  --set existingSecret=rask-secrets \
  --set viewer.image.repository=<registry>/rask-viewer \
  --set frontend.image.repository=<registry>/rask-frontend \
  --set config.RAY_DASHBOARD_URL=http://<ray-head>:8265 \
  --set ingress.host=rask.example.org
```

## Critical constraints

- **Never set `viewer.replicas > 1`** — the orchestrator is an in-process
  singleton; concurrent viewers double-submit jobs.
- The orchestrator stays idle until `config.RASK_ORCHESTRATOR_AUTOSTART=true` or
  an operator calls `POST /api/v1/orchestrator/start`.

See `docs/architecture/deployment.md` and the design spec
`docs/superpowers/specs/2026-06-15-rask-helm-chart-design.md`.
```

- [ ] **Step 5: Verify docs render and lint still passes**

Run: `helm lint chart/ --set existingSecret=x && grep -c "Helm chart" docs/architecture/deployment.md`
Expected: lint PASS and a non-zero grep count.

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/deployment.md CLAUDE.md chart/README.md
git commit -m "docs(chart): document the rask Helm chart, drop no-Helm note"
```

---

## Self-review

- **Spec coverage:** layout (T1), ConfigMap/SA (T2), singleton viewer (T3), frontend (T4), migration hook (T5), ingress split (T6), NOTES + validation (T7), docs update incl. `deployment.md` + `CLAUDE.md` (T8). Secrets-via-existingSecret enforced with `required` in T3/T5. Image-build prerequisite documented in T8 README. ✅
- **Placeholder scan:** every template and command is given in full; no TBDs. ✅
- **Consistency:** helper names (`rask.fullname`, `rask.selectorLabels`, `rask.serviceAccountName`), resource name suffixes (`-viewer`, `-frontend`, `-config`, `-migrate`), and env var names (`RAY_DASHBOARD_URL`, `RASK_ORCHESTRATOR_AUTOSTART`, `DATABASE_URL`) match across tasks. ✅
- **Correction vs spec:** spec mentioned `RASK_RAY_ADDRESS` / `ray://…:10001` for the viewer; the verified config uses `RAY_DASHBOARD_URL` for job submission. Plan uses `RAY_DASHBOARD_URL`; `ray://` is noted as the runner's path only.
```
