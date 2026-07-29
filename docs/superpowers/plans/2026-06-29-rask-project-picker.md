# rask Project Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the rask home page's hardcoded `'Default'` project picker with a live, read-only list of the operator's `Project` resources, served by a new platform control-plane API through the gateway.

**Architecture:** A new stateless rask service (`controlplane`) reads cluster-scoped `Project` CRs (`platform.rask.io/v1alpha1`) via the Kubernetes API and serves `GET /api/projects/`. The gateway path-routes `/api/projects` to it. The home SvelteKit app fetches the list with a server-only remote `query()` via `@rask/api` (through the gateway, using `RASK_GATEWAY_URL` server-side) and renders project cards with phase chips. Created-by-kubectl, read-only for site users.

**Tech Stack:** Python 3.13 + FastAPI + `service-kit` + `kubernetes` client (uv workspace brick); Helm chart; Svelte 5 + SvelteKit 2 + `@rask/api` (valibot) + Bun.

**Spec:** `docs/superpowers/specs/2026-06-29-rask-project-picker-design.md`

## Global Constraints

- **`RASK_API_PREFIX=/api`** at runtime (chart `config`, `.env`, `dev-micro.sh`). The code default `/api/v1` is overridden. All endpoint paths and gateway routes use `/api`. The endpoint is `GET /api/projects/` (trailing slash, mirroring `/api/batches/`).
- **JSON payloads are snake_case** (rask convention: `batch_id`, `arkiv_referenskod`, …). The project DTO uses `created_at`, not `createdAt`.
- **Env vars carry no `ra-`/`ra_` prefix** — `RASK_*` only.
- **JS/TS uses Bun exclusively** (`bun`, `bunx`); **Python uses uv** (`uv run`, `uv sync`). Type-check Python with `uvx ty check` / `make typecheck`; frontend with `bun --cwd <app> run check`.
- **Control-plane port is 8820.**
- **Gateway: the `/api/projects` route MUST be registered before the `/api`→core catch-all** (longest-prefix-first; invariant 3 of rask-services-fleet) or core swallows it.
- **Workspace membership is explicit** — adding the Python brick edits root `pyproject.toml` `[tool.uv.workspace] members` AND root `[tool.pytest.ini_options] testpaths` (tests are not auto-discovered). The new brick is Python-only, so root `package.json` `workspaces` is NOT touched.
- **Every `.svelte` change is validated with the `svelte` MCP autofixer** before it's considered done, and follows the Svelte 5 / rask-frontend canon (data via `query()`+`refresh`, `{#each}` keyed, computed = `$derived`, `@rask/ui` tokens).
- **Verify like it ships:** the final task observes the picker working end-to-end in a browser against the live k3s — SSR 200 is not "done". Do not break the running `rask` release in `default`.
- **Commits:** conventional messages; **never** add a `Co-Authored-By: Claude` trailer.

## File Structure

**New — control-plane brick** (`components/services/controlplane/`):
- `pyproject.toml` — brick package metadata + deps (`service-kit`, `kubernetes`, `uvicorn`).
- `src/controlplane/__init__.py` — entrypoint: `app = make_service_app(...)`.
- `src/controlplane/health.py` — `GET /health`.
- `src/controlplane/schemas.py` — `ProjectDTO`, `ProjectsResponse`.
- `src/controlplane/k8s.py` — `ProjectReader` protocol + `K8sProjectReader`.
- `src/controlplane/service.py` — pure `to_dto` / `list_project_dtos`.
- `src/controlplane/routes.py` — `GET /projects/` + `get_reader` dependency.
- `tests/test_controlplane.py` — mapping unit tests + endpoint/503 TestClient tests.

**New — deployable + packaging:**
- `projects/controlplane/pyproject.toml` — composition (no code).
- `.docker/controlplane.dockerfile` — image.

**New — chart:**
- `chart/templates/controlplane.yaml` — Deployment + Service + ServiceAccount + ClusterRole + ClusterRoleBinding.

**New — frontend:**
- `packages/api/src/projects.ts` — valibot schema + `listProjects`.
- `components/frontends/home/src/hooks.server.ts` — gateway `handleFetch`.
- `components/frontends/home/src/lib/remote/home.remote.ts` — `getProjects` query.

**Modified:**
- root `pyproject.toml` — `members` + `testpaths`.
- `components/services/gateway/src/gateway/__init__.py` — controlplane upstream + `/api/projects` route.
- `components/services/gateway/tests/test_controlplane_route.py` — new test (create).
- `packages/api/src/index.ts` — export `./projects`.
- `components/frontends/home/package.json` — add `@rask/api`.
- `components/frontends/home/src/routes/+page.svelte` — live picker.
- `chart/templates/configmap.yaml` — `RASK_CONTROLPLANE_URL` upstream.
- `chart/values.yaml` — `controlplane:` block.
- `Makefile` — `controlplane` in `COMPOSE_IMAGES`.
- `scripts/dev-micro.sh` — controlplane port + run line.

---

### Task 1: Control-plane brick scaffold (walking skeleton — health only)

Stand up the new service following the stateless `volumes_api` pattern: it boots and serves `/api/health`. No k8s logic yet.

**Files:**
- Create: `components/services/controlplane/pyproject.toml`
- Create: `components/services/controlplane/src/controlplane/__init__.py`
- Create: `components/services/controlplane/src/controlplane/health.py`
- Create: `projects/controlplane/pyproject.toml`
- Create: `components/services/controlplane/tests/test_controlplane.py`
- Modify: `pyproject.toml` (root — `members`, `testpaths`)

**Interfaces:**
- Produces: importable ASGI app `controlplane:app`; `controlplane.health.router` (`GET /health`).

- [ ] **Step 1: Create the brick pyproject**

`components/services/controlplane/pyproject.toml`:
```toml
[project]
name = "controlplane"
version = "0.1.0"
description = "controlplane — read-only Project CR listing for the platform home picker."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "kubernetes>=31",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/controlplane"]

[tool.uv.sources]
service-kit = { workspace = true }
```

- [ ] **Step 2: Create the health router**

`components/services/controlplane/src/controlplane/health.py`:
```python
from fastapi import APIRouter
from pydantic import BaseModel


class Health(BaseModel):
    status: str


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> Health:
    return Health(status="ok")
```

- [ ] **Step 3: Create the entrypoint**

`components/services/controlplane/src/controlplane/__init__.py`:
```python
"""controlplane — read-only listing of operator Project CRs for the home picker.
Stateless: no DB/Lance/Ray/S3; reads the k8s API per request (see routes.py)."""

from controlplane import health
from service_kit import make_service_app

app = make_service_app(title="controlplane", routers=[health.router])
```

> Note: `routes.router` is added in Task 3. Keep `__init__` to health-only for now so the skeleton boots.

- [ ] **Step 4: Create the deployable composition**

`projects/controlplane/pyproject.toml`:
```toml
[project]
name = "controlplane-project"
version = "0.1.0"
description = "Deployable: controlplane service."
requires-python = ">=3.13"
dependencies = ["controlplane"]

[tool.uv.sources]
controlplane = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/controlplane",
    "../../packages/service-kit",
]
```

- [ ] **Step 5: Register the brick in the root workspace + testpaths**

In root `pyproject.toml`, add `"components/services/controlplane",` to the `[tool.uv.workspace] members` list (in the `components/services/*` block), and add `"components/services/controlplane/tests",` to the `[tool.pytest.ini_options] testpaths` list.

Find the exact lines first:
```bash
grep -n "components/services/volumes_api\|testpaths" pyproject.toml
```
Then insert the two lines next to their siblings.

- [ ] **Step 6: Write the failing health test**

`components/services/controlplane/tests/test_controlplane.py`:
```python
"""controlplane tests — health skeleton + (later) project listing."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # RASK_API_PREFIX=/api mirrors the deployed fleet; the shared Settings also
    # *requires* viewer in/out, so set dummies even though controlplane ignores them.
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane import app

    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 7: Sync the workspace and run the test (expect pass after sync)**

Run:
```bash
uv sync
uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -v
```
Expected: `test_health_returns_ok` PASSES (the skeleton boots and serves `/api/health`).

- [ ] **Step 8: Commit**

```bash
git add components/services/controlplane projects/controlplane pyproject.toml uv.lock
git commit -m "feat(controlplane): stateless service skeleton + health"
```

---

### Task 2: Project CR → DTO mapping (pure logic, TDD)

The read model: a reader interface, the concrete k8s reader, and the pure mapping/sorting. Pure functions are unit-tested with canned CR dicts — no apiserver.

**Files:**
- Create: `components/services/controlplane/src/controlplane/schemas.py`
- Create: `components/services/controlplane/src/controlplane/k8s.py`
- Create: `components/services/controlplane/src/controlplane/service.py`
- Modify: `components/services/controlplane/tests/test_controlplane.py`

**Interfaces:**
- Produces:
  - `ProjectDTO(slug, name, team, workload, phase, namespace, created_at: str)`
  - `ProjectsResponse(projects: list[ProjectDTO])`
  - `ProjectReader` protocol with `list_projects() -> list[dict[str, Any]]`
  - `controlplane.service.to_dto(cr: dict) -> ProjectDTO`
  - `controlplane.service.list_project_dtos(reader: ProjectReader) -> list[ProjectDTO]` (sorted by `created_at`)

- [ ] **Step 1: Write the schemas**

`components/services/controlplane/src/controlplane/schemas.py`:
```python
from pydantic import BaseModel


class ProjectDTO(BaseModel):
    slug: str
    name: str
    team: str
    workload: str
    phase: str
    namespace: str
    created_at: str


class ProjectsResponse(BaseModel):
    projects: list[ProjectDTO]
```

- [ ] **Step 2: Write the reader (protocol + k8s impl)**

`components/services/controlplane/src/controlplane/k8s.py`:
```python
"""Kubernetes access for controlplane — read-only listing of Project CRs.

The protocol is the seam: routes depend on `ProjectReader`, tests inject a fake,
production injects `K8sProjectReader`. Keeping the real client construction lazy
(in `__init__`, only built when the dependency is actually resolved) means unit
tests that override the dependency never touch the kubernetes client."""

from typing import Any, Protocol

PROJECT_GROUP = "platform.rask.io"
PROJECT_VERSION = "v1alpha1"
PROJECT_PLURAL = "projects"


class ProjectReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...


class K8sProjectReader:
    """Lists Project CRs via the cluster API (in-cluster config, kubeconfig fallback)."""

    def __init__(self) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._api = client.CustomObjectsApi()

    def list_projects(self) -> list[dict[str, Any]]:
        resp = self._api.list_cluster_custom_object(group=PROJECT_GROUP, version=PROJECT_VERSION, plural=PROJECT_PLURAL)
        items: list[dict[str, Any]] = resp.get("items", [])
        return items
```

- [ ] **Step 3: Write the failing mapping tests**

Append to `components/services/controlplane/tests/test_controlplane.py`:
```python
def _cr(name: str, *, team: str = "t", phase: str | None = "Ready", created: str = "2026-01-01T00:00:00Z") -> dict:
    cr: dict = {
        "metadata": {"name": name, "creationTimestamp": created},
        "spec": {"team": team, "workload": {"type": "htr"}},
    }
    if phase is not None:
        cr["status"] = {"phase": phase, "namespace": f"project-{name}"}
    return cr


def test_to_dto_maps_all_fields() -> None:
    from controlplane.service import to_dto

    dto = to_dto(_cr("demo", team="team-archives", phase="Ready"))
    assert dto.slug == "demo"
    assert dto.name == "demo"
    assert dto.team == "team-archives"
    assert dto.workload == "htr"
    assert dto.phase == "Ready"
    assert dto.namespace == "project-demo"
    assert dto.created_at == "2026-01-01T00:00:00Z"


def test_to_dto_missing_status_defaults_pending() -> None:
    from controlplane.service import to_dto

    dto = to_dto(_cr("fresh", phase=None))
    assert dto.phase == "Pending"
    assert dto.namespace == ""


def test_list_project_dtos_sorted_by_created_at() -> None:
    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict]:
            return [
                _cr("b", created="2026-02-01T00:00:00Z"),
                _cr("a", created="2026-01-01T00:00:00Z"),
            ]

    dtos = list_project_dtos(FakeReader())
    assert [d.slug for d in dtos] == ["a", "b"]
```

- [ ] **Step 4: Run to verify failure**

Run: `uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -k "to_dto or sorted" -v`
Expected: FAIL — `ModuleNotFoundError: controlplane.service` (not written yet).

- [ ] **Step 5: Write the mapping service**

`components/services/controlplane/src/controlplane/service.py`:
```python
"""Pure mapping from raw Project CR dicts to API DTOs. No I/O, no k8s client."""

from typing import Any

from controlplane.k8s import ProjectReader
from controlplane.schemas import ProjectDTO


def to_dto(cr: dict[str, Any]) -> ProjectDTO:
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
        created_at=meta.get("creationTimestamp", ""),
    )


def list_project_dtos(reader: ProjectReader) -> list[ProjectDTO]:
    dtos = [to_dto(cr) for cr in reader.list_projects()]
    return sorted(dtos, key=lambda d: d.created_at)
```

- [ ] **Step 6: Run to verify pass**

Run: `uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -k "to_dto or sorted" -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add components/services/controlplane
git commit -m "feat(controlplane): Project CR -> DTO mapping + reader seam"
```

---

### Task 3: `/api/projects/` endpoint + 503 handling

Wire the route using an injectable reader dependency; test the happy path and the k8s-failure → 503 path with `dependency_overrides`.

**Files:**
- Create: `components/services/controlplane/src/controlplane/routes.py`
- Modify: `components/services/controlplane/src/controlplane/__init__.py`
- Modify: `components/services/controlplane/tests/test_controlplane.py`

**Interfaces:**
- Consumes: `controlplane.service.list_project_dtos`, `controlplane.k8s.ProjectReader`, `ProjectsResponse`.
- Produces: `controlplane.routes.router` (`GET /projects/`), `controlplane.routes.get_reader` (the FastAPI dependency to override in tests).

- [ ] **Step 1: Write the failing endpoint tests**

Append to `components/services/controlplane/tests/test_controlplane.py`:
```python
def test_list_projects_endpoint_returns_dtos(client: TestClient) -> None:
    from controlplane import app
    from controlplane.routes import get_reader

    class FakeReader:
        def list_projects(self) -> list[dict]:
            return [_cr("demo", team="team-archives", phase="Ready")]

    app.dependency_overrides[get_reader] = lambda: FakeReader()
    try:
        resp = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"][0]["slug"] == "demo"
    assert body["projects"][0]["phase"] == "Ready"
    assert body["projects"][0]["created_at"] == "2026-01-01T00:00:00Z"


def test_list_projects_endpoint_503_on_reader_error(client: TestClient) -> None:
    from controlplane import app
    from controlplane.routes import get_reader

    class BoomReader:
        def list_projects(self) -> list[dict]:
            raise RuntimeError("k8s unreachable")

    app.dependency_overrides[get_reader] = lambda: BoomReader()
    try:
        resp = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -k "endpoint" -v`
Expected: FAIL — `404` (route not mounted) / `ImportError` on `get_reader`.

- [ ] **Step 3: Write the route**

`components/services/controlplane/src/controlplane/routes.py`:
```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from controlplane import service
from controlplane.k8s import K8sProjectReader, ProjectReader
from controlplane.schemas import ProjectsResponse

router = APIRouter(prefix="/projects", tags=["projects"])


def get_reader() -> ProjectReader:
    """Build the live k8s reader. Overridden in tests via app.dependency_overrides."""
    return K8sProjectReader()


ReaderDep = Annotated[ProjectReader, Depends(get_reader)]


@router.get("/")
def list_projects(reader: ReaderDep) -> ProjectsResponse:
    try:
        dtos = service.list_project_dtos(reader)
    except Exception as exc:  # noqa: BLE001 - any k8s failure surfaces as a clean 503
        raise HTTPException(status_code=503, detail="cannot reach kubernetes api") from exc
    return ProjectsResponse(projects=dtos)
```

- [ ] **Step 4: Mount the route in the entrypoint**

Edit `components/services/controlplane/src/controlplane/__init__.py` — add `routes` to the imports and the router list:
```python
from controlplane import health, routes
from service_kit import make_service_app

app = make_service_app(title="controlplane", routers=[health.router, routes.router])
```

- [ ] **Step 5: Run to verify pass (full file)**

Run: `uv run --package controlplane pytest components/services/controlplane/tests/test_controlplane.py -v`
Expected: PASS (all: health + 3 mapping + 2 endpoint).

- [ ] **Step 6: Lint + typecheck the brick**

Run:
```bash
uv run ruff check components/services/controlplane
uvx ty check components/services/controlplane
```
Expected: clean. (Fix any annotation/lint findings inline.)

- [ ] **Step 7: Commit**

```bash
git add components/services/controlplane
git commit -m "feat(controlplane): GET /api/projects/ endpoint with 503 on k8s error"
```

---

### Task 4: Containerization + dev-micro

Make the service buildable as an image and runnable in the local fleet.

**Files:**
- Create: `.docker/controlplane.dockerfile`
- Modify: `Makefile` (`COMPOSE_IMAGES`)
- Modify: `scripts/dev-micro.sh`

**Interfaces:**
- Produces: image `controlplane:dev` (uvicorn `controlplane:app` on `:8820`); local fleet process on `:8820`.

- [ ] **Step 1: Create the dockerfile (copy of volumes-api, retargeted)**

`.docker/controlplane.dockerfile` — identical structure to `.docker/volumes-api.dockerfile`, with these substitutions: `--package volumes-api` → `--package controlplane`; the `EXPOSE`/`HEALTHCHECK` port `8803` → `8820`; the title/description labels → controlplane; and the final CMD:
```dockerfile
CMD ["uvicorn", "controlplane:app", \
     "--host", "0.0.0.0", "--port", "8820", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.1"]
```
(Copy the full file from `volumes-api.dockerfile` and apply exactly those edits — the two-stage uv build is unchanged.)

- [ ] **Step 2: Add controlplane to the image build list**

In `Makefile`, add `controlplane` to `COMPOSE_IMAGES`:
```makefile
COMPOSE_IMAGES = gateway core-api search-api volumes-api ray-api orchestrator controlplane
```

- [ ] **Step 3: Add controlplane to the dev fleet**

In `scripts/dev-micro.sh`: add a port default near the other `*_PORT` exports:
```bash
CONTROLPLANE_PORT="${CONTROLPLANE_PORT:-8820}"
```
and add a run line after the `volumes-api` line:
```bash
run controlplane "$CONTROLPLANE_PORT" controlplane:app env RASK_ORCHESTRATOR_AUTOSTART=false
```

- [ ] **Step 4: Build the image to verify it compiles + packages**

Run:
```bash
docker buildx build -f .docker/controlplane.dockerfile -t controlplane:dev --load .
```
Expected: image builds; final stage exports `controlplane:dev`.

- [ ] **Step 5: Commit**

```bash
git add .docker/controlplane.dockerfile Makefile scripts/dev-micro.sh
git commit -m "build(controlplane): dockerfile + fleet image list + dev-micro entry"
```

---

### Task 5: Gateway route for `/api/projects`

Path-route `/api/projects` to the control-plane service, before the core catch-all.

**Files:**
- Modify: `components/services/gateway/src/gateway/__init__.py`
- Create: `components/services/gateway/tests/test_controlplane_route.py`
- Modify: `pyproject.toml` (root testpaths — only if the gateway tests dir isn't already listed)

**Interfaces:**
- Consumes: `RASK_CONTROLPLANE_URL` env (fallback `http://127.0.0.1:8820`).
- Produces: a `/api/projects` route entry whose upstream app-id is `controlplane`, ordered before the `prefix` (core) catch-all.

- [ ] **Step 1: Write the failing route-ordering test**

`components/services/gateway/tests/test_controlplane_route.py`:
```python
"""The /api/projects gateway route must resolve to controlplane, before core."""

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing `gateway` builds the app (make_service_app -> build_settings).
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")


def test_projects_route_present_and_before_core() -> None:
    from gateway import _routes

    routes = _routes()
    prefixes = [r[0] for r in routes]
    assert "/api/projects" in prefixes
    assert prefixes.index("/api/projects") < prefixes.index("/api")


def test_projects_route_targets_controlplane() -> None:
    from gateway import _routes

    proj = next(r for r in _routes() if r[0] == "/api/projects")
    assert proj[1] == "controlplane"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --package gateway pytest components/services/gateway/tests/test_controlplane_route.py -v`
Expected: FAIL — `/api/projects` not in prefixes.

If it errors with "no tests ran / path not collected", add `"components/services/gateway/tests",` to `[tool.pytest.ini_options] testpaths` in root `pyproject.toml` and rerun.

- [ ] **Step 3: Add the upstream + route**

In `components/services/gateway/src/gateway/__init__.py`, inside `_routes()`: add the controlplane upstream alongside the others, and insert its route immediately before the `(prefix, *core)` catch-all:
```python
    orch = ("orchestrator", os.environ.get("RASK_ORCH_API_URL", "http://127.0.0.1:8810"))
    controlplane = ("controlplane", os.environ.get("RASK_CONTROLPLANE_URL", "http://127.0.0.1:8820"))
    # longest / most-specific prefixes first; the prefix itself is the catch-all
    return [
        (f"{prefix}/search", *search),
        (f"{prefix}/volumes", *volumes),
        (f"{prefix}/ray", *ray),
        (f"{prefix}/orchestrator", *orch),
        (f"{prefix}/projects", *controlplane),
        ("/api/serve", *ray),
        (prefix, *core),
        ("/api", *core),
    ]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --package gateway pytest components/services/gateway/tests/test_controlplane_route.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add components/services/gateway pyproject.toml
git commit -m "feat(gateway): route /api/projects to controlplane (before core catch-all)"
```

---

### Task 6: `@rask/api` projects client

Add the valibot-validated client module the frontend will call.

**Files:**
- Create: `packages/api/src/projects.ts`
- Modify: `packages/api/src/index.ts`

**Interfaces:**
- Produces: `Project` type, `ProjectsPayload` type, `listProjects(fetchFn?) -> Promise<ProjectsPayload>` (calls `/api/projects/`).

- [ ] **Step 1: Write the client module**

`packages/api/src/projects.ts`:
```typescript
// @rask/api/projects — read-only list of operator Project resources for the home picker.

import * as v from 'valibot';
import { parse } from './parse.js';

export const ProjectSchema = v.object({
	slug: v.string(),
	name: v.string(),
	team: v.string(),
	workload: v.string(),
	phase: v.string(),
	namespace: v.string(),
	created_at: v.string(),
});
export type Project = v.InferOutput<typeof ProjectSchema>;

export const ProjectsPayloadSchema = v.object({ projects: v.array(ProjectSchema) });
export type ProjectsPayload = v.InferOutput<typeof ProjectsPayloadSchema>;

export async function listProjects(fetchFn: typeof fetch = fetch): Promise<ProjectsPayload> {
	const res = await fetchFn('/api/projects/');
	if (!res.ok) throw new Error(`listProjects: HTTP ${res.status}`);
	return parse(ProjectsPayloadSchema, await res.json());
}
```

- [ ] **Step 2: Export it from the package index**

In `packages/api/src/index.ts`, add `export * from './projects';` alongside the other domain exports.

- [ ] **Step 3: Type-check the package**

Run: `bun --cwd packages/api run check 2>/dev/null || bunx tsc --noEmit -p packages/api`
Expected: no type errors. (If `@rask/api` has no `check` script, the `tsc` form validates it; it's JIT-consumed so a clean typecheck is the gate.)

- [ ] **Step 4: Commit**

```bash
git add packages/api/src/projects.ts packages/api/src/index.ts
git commit -m "feat(api): listProjects client + valibot schema"
```

---

### Task 7: Home page live picker

Wire the home app to fetch and render the live project list: add the `@rask/api` dep, the gateway `handleFetch`, the remote query, and rewrite the picker.

**Files:**
- Modify: `components/frontends/home/package.json`
- Create: `components/frontends/home/src/hooks.server.ts`
- Create: `components/frontends/home/src/lib/remote/home.remote.ts`
- Modify: `components/frontends/home/src/routes/+page.svelte`

**Interfaces:**
- Consumes: `@rask/api` `listProjects`, `Project`, `makeGatewayHandleFetch`; `RASK_GATEWAY_URL` env (server-side).
- Produces: home `/` renders one card per live project with a phase chip; empty state when none.

- [ ] **Step 1: Add `@rask/api` to the home app**

In `components/frontends/home/package.json`, add to `dependencies`:
```json
		"@rask/api": "workspace:*",
```
Then install:
```bash
bun install
```

- [ ] **Step 2: Add the gateway handleFetch hook**

`components/frontends/home/src/hooks.server.ts`:
```typescript
import type { HandleFetch } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { makeGatewayHandleFetch } from '@rask/api';

// SSR reads (remote query() via getRequestEvent().fetch) issue relative /api/*,
// which in prod resolves against the external ingress origin and hairpins back
// through the ingress. Route them straight to the in-cluster gateway instead.
// Dev defaults to the local gateway; the chart sets RASK_GATEWAY_URL in-cluster.
export const handleFetch: HandleFetch = makeGatewayHandleFetch(
	env.RASK_GATEWAY_URL ?? 'http://localhost:8888',
);
```

- [ ] **Step 3: Add the remote query**

`components/frontends/home/src/lib/remote/home.remote.ts`:
```typescript
import { query, getRequestEvent } from '$app/server';
import { listProjects, type Project } from '@rask/api';

// THE ONE PATTERN (rask-frontend canon §1): a server-only query() whose body
// calls a @rask/api function with getRequestEvent().fetch, so SSR resolves the
// relative /api/* against the request (rewritten to the gateway in hooks.server.ts),
// reuses @rask/api's valibot parse, and inlines the result into the SSR payload.
export const getProjects = query(async (): Promise<Project[]> => {
	const { projects } = await listProjects(getRequestEvent().fetch);
	return projects;
});
```

- [ ] **Step 4: Rewrite the picker page**

Replace `components/frontends/home/src/routes/+page.svelte` with:
```svelte
<script lang="ts">
	import { gsap } from 'gsap';
	import { Boxes } from '@lucide/svelte';
	import { getProjects } from '$lib/remote/home.remote';

	// Home / project picker — the pre-project landing at `/`. Projects come from the
	// operator (Project CRs) via the controlplane API through the gateway. Read-only:
	// projects are created with kubectl; opening a project is a later slice, so the
	// cards are not yet click-through.
	const projectsQuery = getProjects();
	// `await` suspends to the <svelte:boundary> pending snippet on first render
	// (svelte.config experimental.async). `.refresh()` could repoll later.
	const projects = $derived(await projectsQuery);

	// Map a Project.phase to a status-chip token class. Ready is the only "live"
	// state; everything mid-provision is muted; Failed is destructive.
	function phaseClass(phase: string): string {
		if (phase === 'Ready') return 'bg-primary/10 text-primary';
		if (phase === 'Failed') return 'bg-destructive/10 text-destructive';
		return 'bg-muted text-muted-foreground';
	}

	// Subtle GSAP stagger reveal of the hero. Client-only; targets static
	// [data-reveal] nodes present at attach time.
	function reveal(node: HTMLElement) {
		const tween = gsap.from(node.querySelectorAll('[data-reveal]'), {
			y: 20,
			opacity: 0,
			duration: 0.6,
			stagger: 0.07,
			ease: 'power2.out',
			clearProps: 'all',
		});
		return () => tween.kill();
	}
</script>

<svelte:head><title>rask — HTR platform</title></svelte:head>

<div class="mx-auto w-full max-w-5xl px-6 pt-28 pb-20" {@attach reveal}>
	<header class="mb-12 max-w-2xl">
		<p data-reveal class="text-muted-foreground mb-3 font-mono text-xs tracking-[0.2em] uppercase">
			Riksarkivet · HTR platform
		</p>
		<h1 data-reveal class="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
			Transcribe the archives.
		</h1>
		<p data-reveal class="text-muted-foreground mt-4 text-base leading-relaxed text-pretty">
			Projects are provisioned by the platform operator. Each runs the image → ALTO pipeline in its
			own isolated workspace.
		</p>
	</header>

	<svelte:boundary>
		<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
			{#each projects as p (p.slug)}
				<div
					class="bg-card flex flex-col rounded-xl border p-5"
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
				</div>
			{:else}
				<div
					class="border-border/70 text-muted-foreground col-span-full flex min-h-[164px] flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-sm"
				>
					No projects yet — create one with <code class="font-mono">kubectl apply</code>.
				</div>
			{/each}
		</div>

		{#snippet pending()}
			<div class="text-muted-foreground p-6">Loading projects…</div>
		{/snippet}

		{#snippet failed(error)}
			<div class="border-destructive/40 bg-destructive/10 text-destructive rounded-xl border p-4 text-sm">
				Couldn't reach the platform: {error instanceof Error ? error.message : String(error)}
			</div>
		{/snippet}
	</svelte:boundary>
</div>
```

- [ ] **Step 5: Validate the component with the svelte MCP autofixer**

Use the `svelte` MCP autofixer on `components/frontends/home/src/routes/+page.svelte` (standing rule). Apply any fixes it reports. Confirm the `{#each}` is keyed (`(p.slug)`), the computed value is `$derived`, and no browser globals sit at the top level.

- [ ] **Step 6: Type-check + gates**

Run:
```bash
bun --cwd components/frontends/home run check
make check
```
Expected: home `svelte-check` clean; `make check` (fmt + lint + typecheck across the repo) green.

- [ ] **Step 7: Commit**

```bash
git add components/frontends/home
git commit -m "feat(home): live project picker via controlplane query (read-only)"
```

---

### Task 8: Helm chart — controlplane Deployment + RBAC + gateway wiring

Deploy the service in the platform chart with its own ServiceAccount + read-only ClusterRole, and give the gateway its upstream URL.

**Files:**
- Create: `chart/templates/controlplane.yaml`
- Modify: `chart/templates/configmap.yaml`
- Modify: `chart/values.yaml`

**Interfaces:**
- Consumes: `image.repository`/`image.tag`/`image.pullPolicy`, `resources.fleet`, `controlplane.{enabled,port}`, the `rask.fullname`/`rask.labels`/`rask.componentLabels`/`rask.selectorLabels` helpers, the `rask-config` ConfigMap.
- Produces: in-cluster Service `rask-controlplane:8820`; `RASK_CONTROLPLANE_URL` in `rask-config`; a ClusterRole granting read on `projects.platform.rask.io`.

- [ ] **Step 1: Add controlplane values**

In `chart/values.yaml`, add a top-level block (e.g. after the `services:` block):
```yaml
# Platform control-plane: read-only listing of operator Project CRs for the
# home picker. Its own ServiceAccount + ClusterRole (read projects only).
controlplane:
  enabled: true
  port: 8820
```

- [ ] **Step 2: Add the gateway upstream to the ConfigMap**

In `chart/templates/configmap.yaml`, add to the "Gateway upstreams" block:
```yaml
  RASK_CONTROLPLANE_URL: {{ printf "http://%s-controlplane:8820" (include "rask.fullname" .) | quote }}
```

- [ ] **Step 3: Create the controlplane template (Deployment + Service + SA + RBAC)**

`chart/templates/controlplane.yaml`:
```yaml
{{- if .Values.controlplane.enabled }}
{{- $name := "controlplane" }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rask.fullname" . }}-controlplane
  labels:
    {{- include "rask.componentLabels" (list . $name) | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      {{- include "rask.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: controlplane
  template:
    metadata:
      annotations:
        checksum/config: {{ include (print .Template.BasePath "/configmap.yaml") . | sha256sum }}
      labels:
        {{- include "rask.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: controlplane
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ include "rask.fullname" . }}-controlplane
      containers:
        - name: controlplane
          image: "{{ .Values.image.repository }}{{ if .Values.image.repository }}/{{ end }}controlplane:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["uvicorn"]
          args:
            - "controlplane:app"
            - "--host=0.0.0.0"
            - "--port={{ .Values.controlplane.port }}"
          ports:
            - name: http
              containerPort: {{ .Values.controlplane.port }}
          envFrom:
            - configMapRef:
                name: {{ include "rask.fullname" . }}-config
          readinessProbe:
            httpGet: {path: /api/health, port: http}
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /api/health, port: http}
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            {{- toYaml .Values.resources.fleet | nindent 12 }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rask.fullname" . }}-controlplane
  labels:
    {{- include "rask.componentLabels" (list . $name) | nindent 4 }}
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ .Values.controlplane.port }}
      targetPort: http
  selector:
    {{- include "rask.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: controlplane
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "rask.fullname" . }}-controlplane
  labels:
    {{- include "rask.labels" . | nindent 4 }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ include "rask.fullname" . }}-controlplane
  labels:
    {{- include "rask.labels" . | nindent 4 }}
rules:
  - apiGroups: ["platform.rask.io"]
    resources: ["projects"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ include "rask.fullname" . }}-controlplane
  labels:
    {{- include "rask.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {{ include "rask.fullname" . }}-controlplane
subjects:
  - kind: ServiceAccount
    name: {{ include "rask.fullname" . }}-controlplane
    namespace: {{ .Release.Namespace }}
{{- end }}
```

- [ ] **Step 4: Render the chart to verify templating**

Run:
```bash
helm template rask ./chart | grep -A2 "kind: ClusterRole" | grep controlplane
helm template rask ./chart | grep "RASK_CONTROLPLANE_URL"
helm template rask ./chart > /dev/null && echo "render OK"
```
Expected: the controlplane ClusterRole + `RASK_CONTROLPLANE_URL` appear; full render succeeds with no template errors.

- [ ] **Step 5: Commit**

```bash
git add chart/templates/controlplane.yaml chart/templates/configmap.yaml chart/values.yaml
git commit -m "feat(chart): controlplane deployment + read-only Project RBAC + gateway URL"
```

---

### Task 9: End-to-end verification on the live k3s

Build the changed images, load them into k3s, upgrade the release, and observe the picker working in a browser. (The operator + `Project demo` from earlier should be present; if `demo` was deleted, re-apply it so the picker has data.)

**Files:** none (verification + deploy).

**Interfaces:** Consumes everything above. Produces: observed-working picker.

- [ ] **Step 1: Ensure a Project exists to display**

Run:
```bash
kubectl get projects.platform.rask.io
```
Expected: `demo` present (Phase `Ready`). If absent: `kubectl apply -k /home/morgan/rask-operator/config/samples/` and wait for `Ready`.

- [ ] **Step 2: Build the three changed images**

Run (native arm64 on the node):
```bash
cd /home/morgan/rask
for s in controlplane gateway home; do
  if [ "$s" = "home" ]; then
    docker buildx build -f .docker/frontend.dockerfile --build-arg APP=home -t home:dev --load .
  else
    docker buildx build -f .docker/$s.dockerfile -t $s:dev --load .
  fi
done
```
Expected: `controlplane:dev`, `gateway:dev`, `home:dev` all build. (Confirm the frontend dockerfile name/arg with `ls .docker/ | grep -i front` and the Makefile `frontend` build loop if the `APP` arg differs.)

- [ ] **Step 3: Import the images into k3s containerd**

Run:
```bash
for s in controlplane gateway home; do
  docker save $s:dev | sudo k3s ctr images import -
done
sudo -n k3s ctr images ls | grep -E "controlplane|gateway|home" | grep ":dev"
```
Expected: all three listed as `:dev`, `linux/arm64`.

- [ ] **Step 4: Upgrade the rask release**

Run (mirror how the release was installed — `make k3s-up` wraps `helm upgrade`; pass the same value overrides):
```bash
helm get values rask -n default -o yaml > /tmp/rask-values.snapshot.yaml   # safety snapshot
make k3s-up   # or: helm upgrade rask ./chart -n default --reuse-values
kubectl -n default rollout status deploy/rask-controlplane --timeout=120s
kubectl -n default rollout status deploy/rask-gateway --timeout=120s
kubectl -n default rollout status deploy/rask-home --timeout=120s
```
Expected: controlplane/gateway/home roll out cleanly. If `make k3s-up` isn't the right wrapper, use the `helm upgrade` form with the release's existing values.

- [ ] **Step 5: Verify the API path through the gateway (in-cluster)**

Run:
```bash
kubectl -n default exec deploy/rask-gateway -- \
  python -c "import urllib.request,sys; print(urllib.request.urlopen('http://rask-controlplane:8820/api/projects/').read().decode())"
kubectl -n default exec deploy/rask-gateway -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8888/api/projects/').read().decode())"
```
Expected: both return JSON `{"projects":[{"slug":"demo",...,"phase":"Ready",...}]}` — first proves the service + RBAC, second proves the gateway route.

- [ ] **Step 6: Verify in a browser (ships-like)**

Open the platform home page (the ingress root `/`). Confirm:
- the `demo` project card renders with a `Ready` phase chip;
- no `'Default'` card is present;
- deleting the project (`kubectl delete project demo`) and reloading shows the empty state; re-applying it brings the card back.

Capture what you observed (screenshot or the rendered card text) — SSR 200 alone is not acceptance.

- [ ] **Step 7: Confirm the controlplane RBAC is scoped (no over-grant)**

Run:
```bash
kubectl get clusterrole rask-controlplane -o jsonpath='{.rules}'; echo
kubectl auth can-i list projects.platform.rask.io \
  --as=system:serviceaccount:default:rask-controlplane
kubectl auth can-i list secrets -A \
  --as=system:serviceaccount:default:rask-controlplane
```
Expected: rule is projects get/list/watch only; `can-i list projects` → `yes`; `can-i list secrets` → `no`.

- [ ] **Step 8: Record the result**

Note the outcome in the plan's progress ledger (and any follow-ups discovered). Do not mark the feature done unless Steps 5 + 6 were observed.

---

## Self-Review

**1. Spec coverage:**
- Control-plane read API (brick + entrypoint, port 8820, GET /api/projects/, CR→DTO, 503) → Tasks 1–3. ✓
- Deployment + dedicated SA + read-only ClusterRole in platform chart → Task 8. ✓
- Gateway route (before core catch-all) + `RASK_CONTROLPLANE_URL` → Tasks 5, 8. ✓
- Home picker via `@rask/api` remote query through the gateway, `RASK_GATEWAY_URL` server-side, phase chips, no `'default'`, read-only/not-click-through, empty + error states → Tasks 6, 7. ✓
- Testing: unit (mapping + 503), gateway routing, frontend gates, live e2e → Tasks 2,3,5,7,9. ✓
- Packaging (dockerfile, Makefile, dev-micro) → Task 4. ✓
- Non-goals (open project, UI create/delete, default-deploy teardown) → out of scope; not implemented. ✓

**2. Placeholder scan:** No "TBD/TODO/handle errors appropriately". Two deliberate copy-from-sibling steps (Task 4 dockerfile, derived from `volumes-api.dockerfile`; Task 9 frontend image build) name the exact source + the exact edits — flagged, not vague.

**3. Type consistency:** `ProjectDTO`/`ProjectsResponse` (Task 2) match the route return (Task 3), the valibot `ProjectSchema`/`ProjectsPayloadSchema` (Task 6), and the `Project[]` the query returns (Task 7). Field names are snake_case (`created_at`) on both sides. `get_reader` (Task 3) is the override seam used in tests (Task 3) — same name. Gateway upstream id `controlplane` (Task 5) matches the Service name suffix and `RASK_CONTROLPLANE_URL` (Tasks 5, 8). Port `8820` is consistent across brick CMD, dockerfile, dev-micro, values, configmap, and gateway fallback.
