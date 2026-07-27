# Openable Projects (Spec I) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a project openable from the picker — clicking `demo` loads its real rask UI (showing demo's data) at `demo.<projectDomain>/default/overview`, served by per-project frontends the operator deploys, with the picker card linking to a URL the controlplane derives from the project's live Ingress.

**Architecture:** The operator's per-project Helm chart gains frontend Deployments/Services (reusing the *existing* MFE images, base `/default/<domain>`) + one per-project `Ingress` (`<proj>.<projectDomain>`: `/api`→project gateway, `/default/<domain>`→each MFE). The controlplane reads each project's live Ingress host and reports a `url`; the home picker card becomes a link. No rask-frontend rework, default stack untouched (both deferred to Spec II).

**Tech Stack:** Helm (operator `charts/project`), Go (kubebuilder operator), Python 3.13 + FastAPI + kubernetes client (controlplane), Svelte 5 + `@rask/api` (picker), Bun.

**Spec:** `docs/superpowers/specs/2026-06-29-openable-projects-design.md`

**Two repos:** tasks are labelled **[rask-operator]** (`/home/morgan/rask-operator`) or **[rask]** (`/home/morgan/rask`, branch `feat/openable-projects`). Paths are absolute.

## Global Constraints

- **Reuse existing MFE images as-is** (base `/default/<domain>`, images `overview:dev`/`compute:dev`/`discover:dev`/`storage:dev`/`train:dev`/`studio:dev`, SSR bun on port 3000). NO rask-frontend changes in this spec.
- **6 domain MFEs per project:** `overview, compute, discover, storage, train, studio`. Entry surface = `overview`.
- **Per-project ingress paths:** `/api`→`<proj>-gateway:8888`; `/default/<domain>`→`<proj>-<domain>:3000`. Host = `<proj>.<projectDomain>`. ingressClassName `traefik` (k3s).
- **Entry path constant `/default/overview`** (Spec II flips it to `/overview`). Keep it a single named constant in the controlplane.
- **`projectDomain` lives in the operator** (renders the Ingress host); the **controlplane derives `url` from the live Ingress host** + entry path (single source of truth). URL **scheme** is controlplane config (`http` local / `https` prod).
- **Do NOT remove or modify the default stack**, and do NOT change MFE base paths or shared nav (that's Spec II).
- Frontend Deployments carry label `platform.rask.io/project: <proj>` (so `WorkloadReady` includes them) + `app.kubernetes.io/component: <domain>`.
- Go: use the operator `Makefile` (`make manifests generate`, `make test`, `make lint`). Python: `uv`. JS/TS: `bun`. Commits: conventional, **no `Co-Authored-By: Claude` trailer**.
- Verify like it ships: the final task observes a project opening with its own data (Host-header curl + browser), not just a 200.

## File Structure

**[rask-operator] new/modified:**
- Create `charts/project/templates/frontends.yaml` — 6 frontend Deployments + Services.
- Create `charts/project/templates/ingress.yaml` — per-project Ingress.
- Modify `charts/project/values.yaml` — `frontends` list, `projectDomain`, `urlScheme` (unused by chart but documented), `frontend` resources/port.
- Modify `internal/controller/helm.go` — `BuildProjectValues` adds `projectDomain`; new `defaultProjectDomain` const + env override.
- Modify `internal/controller/project_controller.go` — add `networking.k8s.io/ingresses` RBAC marker; regenerate `config/rbac/role.yaml`.

**[rask] new/modified (branch `feat/openable-projects`):**
- Modify `components/services/controlplane/src/controlplane/k8s.py` — `ingress_host`.
- Modify `.../controlplane/schemas.py` — `ProjectDTO.url`.
- Modify `.../controlplane/service.py` — entry-path constant + url assembly.
- Modify `.../controlplane/routes.py` — pass scheme.
- Modify `.../controlplane/tests/test_controlplane.py` — url tests.
- Modify `chart/templates/controlplane.yaml` — `ingresses` read RBAC + `RASK_PROJECT_URL_SCHEME` env.
- Modify `packages/api/src/projects.ts` — `url` in schema.
- Modify `components/frontends/home/src/routes/+page.svelte` — card → link when openable.

---

### Task 1 [rask-operator]: Per-project frontend Deployments + Services

Render the 6 domain MFE frontends into each project namespace, reusing existing images, each pointed at the project's own gateway.

**Files:**
- Create: `/home/morgan/rask-operator/charts/project/templates/frontends.yaml`
- Modify: `/home/morgan/rask-operator/charts/project/values.yaml`

**Interfaces:**
- Consumes: `.Values.projectName`, `.Values.image.{tag,pullPolicy}`, `.Release.Namespace`.
- Produces: Deployments/Services named `<proj>-<domain>` (port 3000) labelled `platform.rask.io/project=<proj>`, `app.kubernetes.io/component=<domain>`, for `domain ∈ {overview,compute,discover,storage,train,studio}`.

- [ ] **Step 1: Add the frontends config to values.yaml**

Append to `/home/morgan/rask-operator/charts/project/values.yaml`:
```yaml
# Per-project frontends (the 6 domain MFEs). Reuses the existing images
# (base /default/<domain>); served behind the per-project Ingress (ingress.yaml).
frontend:
  port: 3000
  resources:
    requests: {cpu: "50m", memory: "64Mi"}
    limits: {cpu: "500m", memory: "256Mi"}
  apps:
    - overview
    - compute
    - discover
    - storage
    - train
    - studio
```

- [ ] **Step 2: Create the frontends template**

`/home/morgan/rask-operator/charts/project/templates/frontends.yaml`:
```yaml
{{- $p := .Values.projectName }}
{{- $img := .Values.image }}
{{- $fe := .Values.frontend }}
{{- range $domain := $fe.apps }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $p }}-{{ $domain }}
  namespace: {{ $.Release.Namespace }}
  labels:
    app.kubernetes.io/managed-by: rask-operator
    platform.rask.io/project: {{ $p }}
    app.kubernetes.io/component: {{ $domain }}
spec:
  replicas: 1
  selector:
    matchLabels:
      platform.rask.io/project: {{ $p }}
      app.kubernetes.io/component: {{ $domain }}
  template:
    metadata:
      labels:
        platform.rask.io/project: {{ $p }}
        app.kubernetes.io/component: {{ $domain }}
    spec:
      containers:
        - name: {{ $domain }}
          image: "{{ $domain }}:{{ $img.tag }}"
          imagePullPolicy: {{ $img.pullPolicy }}
          env:
            # SSR reads hit this project's own gateway (not the platform one).
            - name: RASK_GATEWAY_URL
              value: "http://{{ $p }}-gateway:8888"
            - name: PROTOCOL_HEADER
              value: "x-forwarded-proto"
            - name: HOST_HEADER
              value: "x-forwarded-host"
            - name: PORT
              value: {{ $fe.port | quote }}
            - name: HOST
              value: "0.0.0.0"
          ports:
            - name: http
              containerPort: {{ $fe.port }}
          # TCP probe: MFE apps serve at /default/<domain>, so GET "/" 404s; an open
          # port proves the adapter-bun server is alive (mirrors platform frontends.yaml).
          readinessProbe:
            tcpSocket: {port: http}
            initialDelaySeconds: 3
            periodSeconds: 10
          livenessProbe:
            tcpSocket: {port: http}
            initialDelaySeconds: 5
            periodSeconds: 20
          resources:
            requests:
              cpu: {{ $fe.resources.requests.cpu | quote }}
              memory: {{ $fe.resources.requests.memory | quote }}
            limits:
              cpu: {{ $fe.resources.limits.cpu | quote }}
              memory: {{ $fe.resources.limits.memory | quote }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $p }}-{{ $domain }}
  namespace: {{ $.Release.Namespace }}
  labels:
    app.kubernetes.io/managed-by: rask-operator
    platform.rask.io/project: {{ $p }}
    app.kubernetes.io/component: {{ $domain }}
spec:
  type: ClusterIP
  selector:
    platform.rask.io/project: {{ $p }}
    app.kubernetes.io/component: {{ $domain }}
  ports:
    - name: http
      port: {{ $fe.port }}
      targetPort: http
---
{{- end }}
```

- [ ] **Step 3: Verify the template renders 6 frontend Deployments**

Run:
```bash
cd /home/morgan/rask-operator
helm template demo charts/project --set projectName=demo > /tmp/fe-render.yaml
grep -c "kind: Deployment" /tmp/fe-render.yaml   # services(6) + frontends(6) = expect >= 12
for d in overview compute discover storage train studio; do
  grep -q "name: demo-$d" /tmp/fe-render.yaml && echo "ok demo-$d" || echo "MISSING demo-$d"
done
grep -A2 'image: "overview:dev"' /tmp/fe-render.yaml | head -3
grep "RASK_GATEWAY_URL" /tmp/fe-render.yaml | head -1   # -> http://demo-gateway:8888
```
Expected: 6 `demo-<domain>` deployments, image `overview:dev` etc., `RASK_GATEWAY_URL=http://demo-gateway:8888`.

- [ ] **Step 4: Commit**

```bash
cd /home/morgan/rask-operator
git add charts/project/templates/frontends.yaml charts/project/values.yaml
git commit -m "feat(chart): render per-project frontend MFEs (reuse existing images)"
```

---

### Task 2 [rask-operator]: Per-project Ingress + projectDomain + ingress RBAC

Make each project reachable on its own host, and grant the operator permission to manage Ingresses.

**Files:**
- Create: `/home/morgan/rask-operator/charts/project/templates/ingress.yaml`
- Modify: `/home/morgan/rask-operator/charts/project/values.yaml`
- Modify: `/home/morgan/rask-operator/internal/controller/helm.go`
- Modify: `/home/morgan/rask-operator/internal/controller/project_controller.go`
- (generated) `/home/morgan/rask-operator/config/rbac/role.yaml`

**Interfaces:**
- Consumes: `.Values.projectName`, `.Values.projectDomain`, `.Values.frontend.{port,apps}`, the gateway Service `<proj>-gateway:8888`.
- Produces: an `Ingress` named `<proj>` in `project-<proj>`, host `<proj>.<projectDomain>`, labelled `platform.rask.io/project=<proj>`. `BuildProjectValues` now sets `projectDomain`.

- [ ] **Step 1: Add projectDomain to values.yaml**

Append to `/home/morgan/rask-operator/charts/project/values.yaml`:
```yaml
# projectDomain: the wildcard base domain for per-project ingress hosts
# (<project>.<projectDomain>). Local dev: a wildcard dnsmasq *.rask.local -> node IP
# (nothing external). Prod: a real wildcard DNS record + cert. Injected by the operator.
projectDomain: rask.local
ingressClassName: traefik
```

- [ ] **Step 2: Create the ingress template**

`/home/morgan/rask-operator/charts/project/templates/ingress.yaml`:
```yaml
{{- $p := .Values.projectName }}
{{- $fe := .Values.frontend }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ $p }}
  namespace: {{ .Release.Namespace }}
  labels:
    app.kubernetes.io/managed-by: rask-operator
    platform.rask.io/project: {{ $p }}
spec:
  ingressClassName: {{ .Values.ingressClassName }}
  rules:
    - host: {{ printf "%s.%s" $p .Values.projectDomain | quote }}
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: {{ $p }}-gateway
                port:
                  number: 8888
          {{- range $domain := $fe.apps }}
          - path: /default/{{ $domain }}
            pathType: Prefix
            backend:
              service:
                name: {{ $p }}-{{ $domain }}
                port:
                  number: {{ $fe.port }}
          {{- end }}
```

- [ ] **Step 3: Thread projectDomain through the operator (helm.go)**

In `/home/morgan/rask-operator/internal/controller/helm.go`, add a `projectDomain` default (with env override) and set it in `BuildProjectValues`. Replace the `defaultClusterQueue` const block + `BuildProjectValues` with:
```go
import (
	"context"
	"os"

	platformv1alpha1 "github.com/carpelan/rask-operator/api/v1alpha1"
)

// defaultClusterQueue is the Kueue ClusterQueue the per-project LocalQueue borrows from.
const defaultClusterQueue = "rask"

// defaultProjectDomain is the wildcard base domain for per-project ingress hosts.
// Override with RASK_PROJECT_DOMAIN (e.g. a real wildcard domain in prod).
const defaultProjectDomain = "rask.local"

func projectDomain() string {
	if d := os.Getenv("RASK_PROJECT_DOMAIN"); d != "" {
		return d
	}
	return defaultProjectDomain
}
```
and update `BuildProjectValues`:
```go
// BuildProjectValues maps a Project to charts/project Helm values.
func BuildProjectValues(p *platformv1alpha1.Project) map[string]any {
	return map[string]any{
		"projectName":   p.Name,
		"clusterQueue":  defaultClusterQueue,
		"projectDomain": projectDomain(),
	}
}
```
(Leave the `HelmProvisioner`/`ChildReadiness` interfaces unchanged.)

- [ ] **Step 4: Add the ingress RBAC marker**

In `/home/morgan/rask-operator/internal/controller/project_controller.go`, add one line to the `+kubebuilder:rbac` marker block (next to the `deployments` line):
```go
// +kubebuilder:rbac:groups=networking.k8s.io,resources=ingresses,verbs=get;list;watch;create;update;patch;delete
```

- [ ] **Step 5: Regenerate manifests + verify build**

Run:
```bash
cd /home/morgan/rask-operator
make manifests generate
grep -A6 'networking.k8s.io' config/rbac/role.yaml | grep ingresses && echo "rbac ok"
make build
```
Expected: `config/rbac/role.yaml` now has an `ingresses` rule; build clean.

- [ ] **Step 6: Verify the ingress renders with the right host + paths**

Run:
```bash
cd /home/morgan/rask-operator
helm template demo charts/project --set projectName=demo --set projectDomain=rask.local \
  | awk '/kind: Ingress/{f=1} f; /^---/{if(f && seen)exit; if(f)seen=1}' | head -40
```
Expected: `Ingress` `demo` with `host: "demo.rask.local"`, `/api`→`demo-gateway:8888`, and `/default/<domain>`→`demo-<domain>:3000` for all 6 domains.

- [ ] **Step 7: Commit**

```bash
cd /home/morgan/rask-operator
git add charts/project/templates/ingress.yaml charts/project/values.yaml internal/controller/helm.go internal/controller/project_controller.go config/rbac/role.yaml
git commit -m "feat(operator): per-project Ingress + projectDomain config + ingresses RBAC"
```

---

### Task 3 [rask]: controlplane derives project `url` from its live Ingress

Add a `url` to each project DTO, read from the operator-created Ingress host.

**Files:**
- Modify: `/home/morgan/rask/components/services/controlplane/src/controlplane/k8s.py`
- Modify: `/home/morgan/rask/components/services/controlplane/src/controlplane/schemas.py`
- Modify: `/home/morgan/rask/components/services/controlplane/src/controlplane/service.py`
- Modify: `/home/morgan/rask/components/services/controlplane/src/controlplane/routes.py`
- Modify: `/home/morgan/rask/components/services/controlplane/tests/test_controlplane.py`

**Interfaces:**
- Consumes: existing `ProjectReader.list_projects()`, `ProjectsResponse`.
- Produces: `ProjectReader.ingress_host(namespace) -> str | None`; `ProjectDTO.url: str`; `service.list_project_dtos(reader, scheme) -> list[ProjectDTO]`; `service.PROJECT_ENTRY_PATH = "/default/overview"`.

- [ ] **Step 1: Add `url` to the DTO**

In `schemas.py`, add `url` to `ProjectDTO` (after `namespace`):
```python
class ProjectDTO(BaseModel):
    slug: str
    name: str
    team: str
    workload: str
    phase: str
    namespace: str
    url: str
    created_at: str
```

- [ ] **Step 2: Add `ingress_host` to the reader (protocol + impl)**

In `k8s.py`, add to the `ProjectReader` Protocol:
```python
class ProjectReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...
    def ingress_host(self, namespace: str) -> str | None: ...
```
and to `K8sProjectReader.__init__` add the networking client, plus the method:
```python
def __init__(self) -> None:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    self._api = client.CustomObjectsApi()
    self._net = client.NetworkingV1Api()


def ingress_host(self, namespace: str) -> str | None:
    resp = self._net.list_namespaced_ingress(namespace, label_selector="platform.rask.io/project")
    for ing in resp.items:
        for rule in ing.spec.rules or []:
            if rule.host:
                return rule.host
    return None
```

- [ ] **Step 3: Write the failing url tests**

In `test_controlplane.py`, update the fake readers to provide `ingress_host`, and add url assertions. Add a module constant import and two tests:
```python
def test_to_dto_builds_url_from_ingress_host() -> None:
    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [_cr("demo", phase="Ready")]

        def ingress_host(self, namespace: str) -> str | None:
            assert namespace == "project-demo"
            return "demo.rask.local"

    dtos = list_project_dtos(FakeReader(), "http")
    assert dtos[0].url == "http://demo.rask.local/default/overview"


def test_url_empty_when_no_ingress() -> None:
    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [_cr("demo", phase="Provisioning")]

        def ingress_host(self, namespace: str) -> str | None:
            return None

    dtos = list_project_dtos(FakeReader(), "http")
    assert dtos[0].url == ""
```
Also update the EXISTING fake/boom readers in the file (`test_list_project_dtos_sorted_by_created_at`, `test_list_projects_endpoint_returns_dtos`, `test_list_projects_endpoint_503_on_reader_error`) to add an `ingress_host` method returning `None` (so they satisfy the protocol), and update `test_list_projects_endpoint_returns_dtos` to assert `body["projects"][0]["url"] == ""` (its fake returns no ingress).

- [ ] **Step 4: Run to verify failure**

Run: `cd /home/morgan/rask && uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -k "url or sorted or endpoint" -v`
Expected: FAIL — `list_project_dtos` takes 1 arg / `ProjectDTO` has no `url`.

- [ ] **Step 5: Implement the service mapping**

Replace `service.py` body with the url-aware version:
```python
"""Pure mapping from raw Project CR dicts (+ live Ingress host) to API DTOs."""

from typing import Any

from controlplane.k8s import ProjectReader
from controlplane.schemas import ProjectDTO

# The project entry surface. Reused MFE images serve at /default/<domain> today;
# Spec II (drop /default) flips this to "/overview".
PROJECT_ENTRY_PATH = "/default/overview"


def _namespace(cr: dict[str, Any]) -> str:
    status = cr.get("status", {})
    ns = status.get("namespace", "")
    if ns:
        return ns
    name = cr.get("metadata", {}).get("name", "")
    return f"project-{name}" if name else ""


def to_dto(cr: dict[str, Any], url: str) -> ProjectDTO:
    meta = cr.get("metadata", {})
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    return ProjectDTO(
        slug=meta.get("name", ""),
        name=meta.get("name", ""),
        team=spec.get("team", ""),
        workload=spec.get("workload", {}).get("type", ""),
        phase=status.get("phase") or "Pending",
        namespace=status.get("namespace", ""),
        url=url,
        created_at=meta.get("creationTimestamp", ""),
    )


def list_project_dtos(reader: ProjectReader, scheme: str) -> list[ProjectDTO]:
    dtos: list[ProjectDTO] = []
    for cr in reader.list_projects():
        ns = _namespace(cr)
        host = reader.ingress_host(ns) if ns else None
        url = f"{scheme}://{host}{PROJECT_ENTRY_PATH}" if host else ""
        dtos.append(to_dto(cr, url))
    return sorted(dtos, key=lambda d: d.created_at)
```

- [ ] **Step 6: Pass the scheme from the route**

In `routes.py`, import `os`, read the scheme, and pass it:
```python
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from controlplane import service
from controlplane.k8s import K8sProjectReader, ProjectReader
from controlplane.schemas import ProjectsResponse

router = APIRouter(prefix="/projects", tags=["projects"])


def get_reader() -> ProjectReader:
    return K8sProjectReader()


ReaderDep = Annotated[ProjectReader, Depends(get_reader)]


@router.get("/")
def list_projects(reader: ReaderDep) -> ProjectsResponse:
    scheme = os.environ.get("RASK_PROJECT_URL_SCHEME", "http")
    try:
        dtos = service.list_project_dtos(reader, scheme)
    except Exception as exc:  # broad catch: any k8s failure surfaces as a clean 503
        raise HTTPException(status_code=503, detail="cannot reach kubernetes api") from exc
    return ProjectsResponse(projects=dtos)
```

- [ ] **Step 7: Run to verify pass + lint/typecheck**

Run:
```bash
cd /home/morgan/rask
uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -v
uv run ruff check components/services/controlplane
uvx ty check components/services/controlplane
```
Expected: all pass; lint+typecheck clean. (The pre-existing StarletteDeprecationWarning is upstream noise.)

- [ ] **Step 8: Commit**

```bash
cd /home/morgan/rask
git add components/services/controlplane
git commit -m "feat(controlplane): derive project url from live Ingress host"
```

---

### Task 4 [rask]: controlplane chart — Ingress read RBAC + URL scheme

Let the in-cluster controlplane read Ingresses and know the URL scheme.

**Files:**
- Modify: `/home/morgan/rask/chart/templates/controlplane.yaml`

**Interfaces:**
- Consumes: the controlplane ClusterRole + Deployment from the picker slice.
- Produces: ClusterRole grants `networking.k8s.io/ingresses` get/list/watch; Deployment sets `RASK_PROJECT_URL_SCHEME`.

- [ ] **Step 1: Add the ingresses rule to the ClusterRole**

In `/home/morgan/rask/chart/templates/controlplane.yaml`, extend the ClusterRole `rules:` (which currently has only the `platform.rask.io/projects` rule) with:
```yaml
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch"]
```

- [ ] **Step 2: Set the URL scheme env on the Deployment**

In the same file, add to the controlplane container `env` (create an `env:` block if absent, alongside `envFrom`):
```yaml
          env:
            - name: RASK_PROJECT_URL_SCHEME
              value: {{ .Values.controlplane.urlScheme | default "http" | quote }}
```
and add `urlScheme: http` under the `controlplane:` block in `/home/morgan/rask/chart/values.yaml`:
```yaml
controlplane:
  enabled: true
  port: 8820
  urlScheme: http
```

- [ ] **Step 3: Verify render**

Run:
```bash
cd /home/morgan/rask
helm template rask ./chart | grep -A3 'resources:\s*\["ingresses"\]' || \
  helm template rask ./chart | grep -B2 -A4 "ingresses"
helm template rask ./chart | grep "RASK_PROJECT_URL_SCHEME" -A1
helm template rask ./chart > /dev/null && echo "render OK"
```
Expected: the ingresses read rule appears in the controlplane ClusterRole; `RASK_PROJECT_URL_SCHEME` env present; full render clean.

- [ ] **Step 4: Commit**

```bash
cd /home/morgan/rask
git add chart/templates/controlplane.yaml chart/values.yaml
git commit -m "feat(chart): controlplane ingresses-read RBAC + project URL scheme"
```

---

### Task 5 [rask]: `@rask/api` url field + picker cards become links

**Files:**
- Modify: `/home/morgan/rask/packages/api/src/projects.ts`
- Modify: `/home/morgan/rask/components/frontends/home/src/routes/+page.svelte`

**Interfaces:**
- Consumes: controlplane `url` field (Task 3).
- Produces: `Project.url: string`; openable picker cards.

- [ ] **Step 1: Add `url` to the valibot schema**

In `/home/morgan/rask/packages/api/src/projects.ts`, add `url` to `ProjectSchema` (after `namespace`):
```typescript
export const ProjectSchema = v.object({
	slug: v.string(),
	name: v.string(),
	team: v.string(),
	workload: v.string(),
	phase: v.string(),
	namespace: v.string(),
	url: v.string(),
	created_at: v.string(),
});
```

- [ ] **Step 2: Make the card a link when openable**

In `/home/morgan/rask/components/frontends/home/src/routes/+page.svelte`, replace the project card `<div>` (the one inside `{#each projects as p (p.slug)}`) so it renders an `<a>` when the project is Ready and has a url, else the existing non-clickable `<div>`. Replace the card block with:
```svelte
			{#each projects as p (p.slug)}
				{#if p.phase === 'Ready' && p.url}
					<a
						href={p.url}
						data-sveltekit-reload
						class="bg-card hover:border-primary/50 flex flex-col rounded-xl border p-5 transition-colors hover:shadow-lg hover:shadow-black/5"
					>
						<div
							class="bg-primary/10 text-primary mb-3 flex size-10 items-center justify-center rounded-lg"
						>
							<Boxes class="size-5" />
						</div>
						<div class="flex items-center justify-between gap-2">
							<div class="font-medium">{p.name}</div>
							<span class="rounded-full px-2 py-0.5 text-xs font-medium {phaseClass(p.phase)}">
								{p.phase}
							</span>
						</div>
						<div class="text-muted-foreground text-sm">{p.team} · {p.workload}</div>
					</a>
				{:else}
					<div class="bg-card flex flex-col rounded-xl border p-5">
						<div
							class="bg-primary/10 text-primary mb-3 flex size-10 items-center justify-center rounded-lg"
						>
							<Boxes class="size-5" />
						</div>
						<div class="flex items-center justify-between gap-2">
							<div class="font-medium">{p.name}</div>
							<span class="rounded-full px-2 py-0.5 text-xs font-medium {phaseClass(p.phase)}">
								{p.phase}
							</span>
						</div>
						<div class="text-muted-foreground text-sm">{p.team} · {p.workload}</div>
					</div>
				{/if}
			{:else}
				<div
					class="border-border/70 text-muted-foreground col-span-full flex min-h-[164px] flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-sm"
				>
					No projects yet — create one with <code class="font-mono">kubectl apply</code>.
				</div>
			{/each}
```

- [ ] **Step 3: Validate the component + type-check**

- Validate `components/frontends/home/src/routes/+page.svelte` with the `svelte` MCP autofixer if available (standing rule); if the MCP isn't registered in this environment, say so in the report and rely on `svelte-check`.

Run:
```bash
cd /home/morgan/rask
bun run --filter @rask/api check
bun run --filter home check
```
Expected: both exit 0 (the home `svelte-check` reports 0 errors / 0 warnings; keyed `{#each}` preserved, `$derived`/`phaseClass` unchanged).

- [ ] **Step 4: Commit**

```bash
cd /home/morgan/rask
git add packages/api/src/projects.ts components/frontends/home/src/routes/+page.svelte
git commit -m "feat(home): open project cards link to project.url when Ready"
```

---

### Task 6 [rask + rask-operator]: Live end-to-end on k3s

Deploy both repos' changes and observe a project opening with its own data. (Controller-run; not a fresh implementer.)

**Files:** none (build + deploy + verify).

- [ ] **Step 1: Rebuild + import the operator image (embeds the updated chart)**

```bash
cd /home/morgan/rask-operator
make docker-build IMG=rask-operator:dev
docker save rask-operator:dev | sudo k3s ctr images import -
make deploy IMG=rask-operator:dev
kubectl -n rask-operator-system rollout restart deploy/rask-operator-controller-manager
kubectl -n rask-operator-system rollout status deploy/rask-operator-controller-manager --timeout=120s
```

- [ ] **Step 2: Re-provision the project so the new chart applies**

The operator reconciles the existing `Project demo` and runs `helm upgrade` with the new templates (frontends + ingress). Force a reconcile and confirm the new children:
```bash
kubectl annotate project demo rask.io/rev="spec-i-$(date +%s)" --overwrite
sleep 20
kubectl -n project-demo get deploy | grep -E "overview|compute|discover|storage|train|studio"
kubectl -n project-demo get ingress
kubectl -n project-demo get ingress demo -o jsonpath='{.spec.rules[0].host}{"\n"}'   # demo.rask.local
```
Expected: 6 frontend deployments, an Ingress `demo` with host `demo.rask.local`. (The MFE images `overview:dev` etc. are already in k3s from the platform install.)

- [ ] **Step 3: Verify the project UI is reachable (Host-header curl, no DNS needed)**

```bash
NODE_IP=$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
echo "node: $NODE_IP"
# overview MFE through the per-project ingress:
curl -s -H "Host: demo.rask.local" "http://$NODE_IP/default/overview" -o /tmp/proj-overview.html -w "%{http_code}\n"
grep -ci "overview\|rask" /tmp/proj-overview.html
# the project's API through the per-project ingress -> demo-gateway:
curl -s -H "Host: demo.rask.local" "http://$NODE_IP/api/batches/" -w "  <- /api/batches\n" | head -c 200
```
Expected: `/default/overview` returns 200 and the overview app HTML; `/api/batches/` returns demo's batches JSON (proving the ingress → demo-gateway → demo backend path).

- [ ] **Step 4: Rebuild + deploy the rask controlplane (url field + RBAC)**

```bash
cd /home/morgan/rask
for s in controlplane home; do
  if [ "$s" = "home" ]; then
    docker buildx build -f .docker/frontend.dockerfile --build-arg APP=home -t home:dev --load .
  else
    docker buildx build -f .docker/$s.dockerfile -t $s:dev --load .
  fi
  docker save $s:dev | sudo k3s ctr images import -
done
make k3s-up   # helm upgrade picks up the controlplane RBAC + scheme env
kubectl -n default rollout restart deploy/rask-controlplane deploy/rask-home
kubectl -n default rollout status deploy/rask-controlplane --timeout=120s
kubectl -n default rollout status deploy/rask-home --timeout=120s
```

- [ ] **Step 5: Verify controlplane reports the url**

```bash
kubectl -n default exec deploy/rask-gateway -c gateway -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8888/api/projects/').read().decode())"
```
Expected: `{"projects":[{"slug":"demo",...,"url":"http://demo.rask.local/default/overview",...}]}`.

- [ ] **Step 6: Verify the picker renders an openable card**

```bash
kubectl -n default exec deploy/rask-gateway -c gateway -- python -c "
import urllib.request
req=urllib.request.Request('http://rask-home:3000/',headers={'x-forwarded-proto':'http','x-forwarded-host':'localhost'})
print(urllib.request.urlopen(req,timeout=45).read().decode())" > /tmp/home.html 2>&1
grep -c 'href="http://demo.rask.local/default/overview"' /tmp/home.html
```
Expected: ≥1 — the demo card is an `<a>` to the project URL. (The SSR payload also inlines `url` in the getProjects data.)

- [ ] **Step 7: Browser confirmation + record**

For a real browser check, the per-project host must resolve to the node IP. Either set up the local wildcard `dnsmasq` (`*.rask.local` → `$NODE_IP`) or add a temporary hosts entry `demo.rask.local <node-ip>`, then open `http://demo.rask.local/default/overview` and the picker, click `demo`, and confirm its overview loads with demo's data. Record what was observed (Step 3 + 5 + 6 outputs at minimum; browser screenshot if available). SSR 200 alone is not acceptance.

- [ ] **Step 8: Restore the cluster + note follow-ups**

Confirm the default release is intact (`kubectl -n default get pods | grep -c Running`) and the `rask` release healthy. Record results + any follow-ups (e.g. Spec II: drop `/default` base + nav rework + strip default) in the ledger.

---

## Self-Review

**1. Spec coverage:**
- Operator per-project frontends → Task 1. ✓
- Operator per-project Ingress + `projectDomain` + ingress RBAC → Task 2. ✓
- controlplane `url` from live Ingress host (+ entry-path constant, scheme config) → Task 3. ✓
- controlplane chart RBAC (ingresses read) + scheme env → Task 4. ✓
- `@rask/api` `url` + picker click-through (Ready+url → link) → Task 5. ✓
- Reuse existing images / no frontend rework / default untouched → honored across Tasks 1–6 (no MFE base or platform-chart-strip changes). ✓
- Local `dnsmasq` / nothing external → Task 6 Step 7 (Host-header curl needs no external DNS; browser uses local dnsmasq/hosts). ✓
- Live e2e with real data → Task 6. ✓
- Out of scope (drop `/default`, strip default) → explicitly deferred to Spec II. ✓

**2. Placeholder scan:** No TBD/TODO/"handle errors". Task 6 is verification with concrete commands. Step 7's browser step names the exact two resolution options.

**3. Type consistency:** `ProjectDTO.url` (Task 3) ↔ valibot `ProjectSchema.url` (Task 5) ↔ controlplane JSON `url` (snake-free, single word). `list_project_dtos(reader, scheme)` signature (Task 3) matches its call in `routes.py` (Task 3 Step 6). `ingress_host(namespace)` defined in the Protocol + impl + all fakes (Task 3). Entry path constant `PROJECT_ENTRY_PATH="/default/overview"` used once. Ingress name `<proj>` + label `platform.rask.io/project` (Task 2) is what `ingress_host`'s `label_selector` matches (Task 3) and what `WorkloadReady` already keys on. Gateway service `<proj>-gateway:8888` consistent across frontends env (Task 1), ingress `/api` backend (Task 2), and the e2e checks (Task 6).
