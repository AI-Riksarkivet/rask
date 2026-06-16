# Extract ray-api + shared ray-kit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all Ray code into a new shared `packages/ray-kit` (schemas + the dashboard/SDK service + errors + `build_client`), then make `ray-api` a thin viewer-free FastAPI shell over it. Deps become `service-kit + ray-kit + httpx + uvicorn` — no `viewer`, no DB/Lance/sqlmodel.

**Architecture:** Strangler cycle 3. `ray-kit` is the cohesive Ray library both `ray-api` and the (still-in-viewer) orchestrator import. `ray-api` owns only the FastAPI shell (routes, proxy, lifespan, health). `viewer` keeps running the orchestrator but imports Ray bits from `ray-kit`.

**Tech Stack:** Python 3.13, uv workspace, hatchling, FastAPI, Ray Job SDK + dashboard HTTP, `ty`, pytest (importlib mode).

---

## Notes for the implementer

- Run from repo root `/home/morgan/rask`. Branch is already `refactor/extract-ray-api`.
- **Ray/uv gotcha:** any `uv run` importing `viewer`/`ray` needs `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and `--no-sync`. App imports need `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` set.
- **Never run pytest with `-o addopts=""`** (drops `--import-mode=importlib`). Quiet runs: `... pytest -m "not slow" -p no:cacheprovider --no-header -q`.
- Commit rules for EVERY commit: no `Co-Authored-By`, no Claude/AI mention, exact message given, do not push.
- Behaviour-preserving refactor. Baseline: full `not slow` suite is green on this branch — **106 passed** (verify once: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q`).
- **Do NOT stage unrelated files.** `.claude/skills/**` may show modified — never `git add -A`; stage explicit paths.

## File structure (end state)

```
packages/ray-kit/src/ray_kit/
  __init__.py     # re-exports build_client, RAY_TRANSIENT_ERRORS
  schemas.py      # ← git mv from viewer/schemas/ray.py (verbatim)
  dashboard.py    # ← git mv from viewer/services/ray_dashboard.py (schema import repointed)
packages/ray-kit/pyproject.toml

components/services/ray_api/src/ray_api/
  __init__.py     # thin shell: make_service_app(routers=[health,routes], proxy_router=proxy.router, lifespan)
  lifespan.py     # build app.state.http + app.state.ray_client (ray_kit.build_client)
  dependencies.py # get_http/get_ray_client + HttpDep/RayClientDep
  routes.py       # ← the /ray router from viewer endpoints/ray.py (calls ray_kit.dashboard)
  proxy.py        # ← the proxy_router (/api/serve/*) from viewer endpoints/ray.py
  health.py       # own health router
components/services/ray_api/tests/{conftest.py,test_ray.py}

# deleted from viewer in Task 2: api/v1/endpoints/ray.py
# repointed in viewer (Task 1): core/lifespan.py, services/submission.py,
#   services/orchestrator/{loop,derive}.py  (+ endpoints/ray.py temporarily)
```

---

## Task 1: Create `packages/ray-kit`; repoint viewer to it

Goal: introduce `ray-kit` (Ray schemas + dashboard service + errors + build_client) and make `viewer` consume it. `viewer` keeps serving `/ray` this task (its `endpoints/ray.py` is repointed to `ray_kit` but not yet moved); `ray_api` stays a facade. Suite stays green.

**Files:**
- Create: `packages/ray-kit/src/ray_kit/{__init__,schemas,dashboard}.py`, `packages/ray-kit/pyproject.toml`
- Modify: root `pyproject.toml` (workspace members); `components/services/viewer/pyproject.toml`
- Modify: `viewer/core/lifespan.py`, `viewer/services/submission.py`, `viewer/services/orchestrator/loop.py`, `viewer/services/orchestrator/derive.py`, `viewer/api/v1/endpoints/ray.py`
- Modify: `projects/{core-api,orchestrator,ray-api,viewer}/pyproject.toml` (add ray-kit member)

- [ ] **Step 1: Move the two Ray modules into ray-kit via `git mv`**

```bash
mkdir -p packages/ray-kit/src/ray_kit
git mv components/services/viewer/src/viewer/schemas/ray.py        packages/ray-kit/src/ray_kit/schemas.py
git mv components/services/viewer/src/viewer/services/ray_dashboard.py packages/ray-kit/src/ray_kit/dashboard.py
```
`schemas.py` is pure `pydantic` + `from ray.dashboard.modules.job.common import JobStatus` — **no import change needed**.

In `packages/ray-kit/src/ray_kit/dashboard.py`, change ONLY its schema import block from:
```python
from viewer.schemas.ray import (
```
to:
```python
from ray_kit.schemas import (
```
(The imported names list and everything else stay identical.)

- [ ] **Step 2: Create `packages/ray-kit/src/ray_kit/__init__.py`**

```python
"""ray-kit — Ray Job SDK + Dashboard HTTP wrapper (schemas, dashboard service,
shared transient-error tuple, client constructor). Used by ray-api and by the
viewer orchestrator. No FastAPI, no viewer, no DB."""

from ray.job_submission import JobSubmissionClient

from ray_kit.dashboard import RAY_TRANSIENT_ERRORS, build_client


__all__ = ["RAY_TRANSIENT_ERRORS", "JobSubmissionClient", "build_client"]
```
(`JobSubmissionClient` is re-exported so `ray-api` can type its dependency on it without a
direct `ray` import — ray-api touches Ray only through `ray-kit`.)

- [ ] **Step 3: Create `packages/ray-kit/pyproject.toml`**

```toml
[project]
name = "ray-kit"
version = "0.1.0"
description = "Ray Job SDK + Dashboard HTTP wrapper (schemas, dashboard service, build_client)."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "pydantic>=2.7",
    "ray>=2.55",
    "requests>=2.31",
    "httpx>=0.27",
    "anyio>=4.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ray_kit"]
```
(If `ray`'s pinned version elsewhere differs, match viewer's `lancedb`-style constraint — viewer pins `ray` already; use a compatible lower bound. `ray>=2.55` matches the cluster's 2.55.1.)

- [ ] **Step 4: Register ray-kit in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"packages/ray-kit"` (alongside `packages/htr`, `packages/storage`, `packages/service-kit`). It is a Python-only package, so it does NOT go in root `package.json` `workspaces`.

- [ ] **Step 5: Add ray-kit as a viewer dependency**

In `components/services/viewer/pyproject.toml`: add `"ray-kit"` to `dependencies`, and add `ray-kit = { workspace = true }` to `[tool.uv.sources]`. (Keep viewer's existing `ray` direct dep — `submission.py`/`loop.py`/`derive.py` still import `JobSubmissionClient`/`JobStatus` directly.)

- [ ] **Step 6: Repoint viewer's importers to ray-kit**

`viewer/core/lifespan.py` — change:
```python
from viewer.services.ray_dashboard import build_client as build_ray_client
```
to:
```python
from ray_kit import build_client as build_ray_client
```

`viewer/services/submission.py` — change:
```python
from viewer.services.ray_dashboard import RAY_TRANSIENT_ERRORS
```
to:
```python
from ray_kit import RAY_TRANSIENT_ERRORS
```

`viewer/services/orchestrator/loop.py` — change:
```python
from viewer.services.ray_dashboard import build_client
```
to:
```python
from ray_kit import build_client
```

`viewer/services/orchestrator/derive.py` — change these two lines:
```python
from viewer.schemas.ray import RayJob
from viewer.services import ray_dashboard
```
to:
```python
from ray_kit import dashboard as ray_dashboard
from ray_kit.schemas import RayJob
```
(So `ray_dashboard.list_jobs(...)` and `ray_dashboard.RAY_TRANSIENT_ERRORS` keep working unchanged. Keep alphabetical import order per ruff.)

`viewer/api/v1/endpoints/ray.py` (still in viewer this task) — change:
```python
from viewer.schemas.ray import (
```
to `from ray_kit.schemas import (` (names unchanged); and change:
```python
from viewer.services import ray_dashboard
```
to:
```python
from ray_kit import dashboard as ray_dashboard
```
(Leave `from viewer.api.dependencies import HttpDep, RayClientDep, SettingsDep` as-is for now.)

- [ ] **Step 7: Add ray-kit to the member list of every project whose closure includes viewer**

In EACH of `projects/core-api/pyproject.toml`, `projects/orchestrator/pyproject.toml`, `projects/ray-api/pyproject.toml`, `projects/viewer/pyproject.toml`: add `"../../packages/ray-kit"` to the `[tool.uv.workspace] members` list. (No `[tool.uv.sources]` entry needed — ray-kit is a transitive dep of viewer, not a direct project dep; the workspace member entry is what lets uv resolve viewer's `ray-kit = { workspace = true }`.)

- [ ] **Step 8: Verify nothing in viewer still references the moved modules, resolve, test**

```bash
grep -rn "viewer.schemas.ray\|viewer.services.ray_dashboard\|from viewer.services import ray_dashboard" components/services/viewer/src && echo "RESIDUAL viewer ray import (fail)" || echo "clean — viewer imports Ray from ray_kit"
grep -rn "import viewer\|from viewer" packages/ray-kit/src && echo "ray-kit IMPORTS VIEWER (fail)" || echo "clean — ray-kit has no viewer import"
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import ray_kit, gateway, core_api, search_api, volumes_api, ray_api, orchestrator, viewer.main; print('all import OK')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/ray-kit packages/service-kit components/services
uvx ruff check packages/ray-kit components/services/viewer
```
Expected: "clean — viewer imports Ray from ray_kit"; "clean — ray-kit has no viewer import"; `all import OK`; suite **106 passed** (unchanged — no tests added/removed); `ty` no new diagnostics beyond the known 23 baseline; `ruff` clean (the 2 pre-existing E501s in `viewer/main.py` + `orchestrator/loop.py` are out of scope — confirm only those remain, if any). If the suite count isn't 106, investigate before committing.

- [ ] **Step 9: Commit**

```bash
git add packages/ray-kit pyproject.toml components/services/viewer projects/core-api projects/orchestrator projects/ray-api projects/viewer uv.lock
git commit -m "refactor(ray-kit): new shared Ray library (schemas + dashboard + build_client); viewer imports from it"
```
After committing, run `git status --short` and confirm no `.claude/skills/**` file was committed.

---

## Task 2: Build the independent `ray-api` shell; cut viewer's ray endpoint

Goal: `ray-api` owns its routes/proxy/lifespan/health over `ray-kit` and drops `viewer`; viewer stops serving `/ray` + `/api/serve/*`.

**Files:**
- Create: `components/services/ray_api/src/ray_api/{lifespan,dependencies,routes,proxy,health}.py`
- Modify: `components/services/ray_api/src/ray_api/__init__.py`, `components/services/ray_api/pyproject.toml`, `projects/ray-api/pyproject.toml`
- Create: `components/services/ray_api/tests/{conftest.py,test_ray.py}`
- Modify: root `pyproject.toml` (testpaths)
- Delete: `viewer/api/v1/endpoints/ray.py`
- Modify: `viewer/api/v1/router.py`, `viewer/main.py`

- [ ] **Step 1: Create `ray_api/dependencies.py`**

```python
"""ray-api DI: the Ray Job SDK client + the dashboard HTTP client, from app.state."""

from typing import Annotated

import httpx
from fastapi import Depends, Request

from ray_kit import JobSubmissionClient


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_ray_client(request: Request) -> JobSubmissionClient | None:
    return request.app.state.ray_client


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RayClientDep = Annotated[JobSubmissionClient | None, Depends(get_ray_client)]
```

- [ ] **Step 2: Create `ray_api/lifespan.py`**

```python
"""ray-api lifespan — build the dashboard HTTP client + the Ray Job SDK client
on app.state. No DB/Lance/S3/orchestrator. Tolerant of an unreachable dashboard
(build_client returns None)."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from anyio import to_thread
from fastapi import FastAPI

from ray_kit import build_client
from service_kit.config import Settings


log = logging.getLogger(__name__)


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)
        app.state.ray_client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)
        log.info("startup_complete")
        try:
            yield
        finally:
            await app.state.http.aclose()
            log.info("shutdown_complete")

    return lifespan
```

- [ ] **Step 3: Create `ray_api/routes.py`** (the `/ray` router from viewer's `endpoints/ray.py`, repointed)

```python
"""Ray Dashboard endpoints — viewer's normalized `/api/v1/ray/*` (health, jobs,
cluster, …). Thin shell over ray_kit.dashboard."""

from fastapi import APIRouter

from ray_api.dependencies import HttpDep, RayClientDep
from ray_kit import dashboard
from ray_kit.schemas import (
    RayActorsPayload,
    RayClusterPayload,
    RayHealth,
    RayJobLogsPayload,
    RayJobsPayload,
    RayLogsPayload,
    RayOverviewPayload,
    RayTasksPayload,
)
from service_kit.dependencies import SettingsDep


router = APIRouter(prefix="/ray", tags=["ray"])


@router.get("/health")
async def ray_health(client: RayClientDep, settings: SettingsDep) -> RayHealth:
    return await dashboard.health(client, settings.ray_dashboard_url)


@router.get("/jobs")
async def ray_jobs(client: RayClientDep, settings: SettingsDep) -> RayJobsPayload:
    return await dashboard.list_jobs(client, settings.ray_dashboard_url)


@router.get("/jobs/{submission_id}/logs")
async def ray_job_logs(client: RayClientDep, submission_id: str, tail: int = 2000) -> RayJobLogsPayload:
    return await dashboard.job_logs(client, submission_id, tail)


@router.get("/cluster")
async def ray_cluster(http: HttpDep, settings: SettingsDep) -> RayClusterPayload:
    return await dashboard.cluster_status(http, settings.ray_dashboard_url)


@router.get("/actors")
async def ray_actors(http: HttpDep, settings: SettingsDep) -> RayActorsPayload:
    return await dashboard.list_actors(http, settings.ray_dashboard_url)


@router.get("/tasks")
async def ray_tasks(http: HttpDep, settings: SettingsDep) -> RayTasksPayload:
    return await dashboard.list_tasks(http, settings.ray_dashboard_url)


@router.get("/overview")
async def ray_overview(http: HttpDep, settings: SettingsDep) -> RayOverviewPayload:
    return await dashboard.overview(http, settings.ray_dashboard_url)


@router.get("/logs")
async def ray_logs(
    http: HttpDep,
    settings: SettingsDep,
    node_id: str,
    filename: str | None = None,
    lines: int = 200,
) -> RayLogsPayload:
    return await dashboard.logs(http, settings.ray_dashboard_url, node_id, filename, lines)
```

- [ ] **Step 4: Create `ray_api/proxy.py`** (the `proxy_router` from viewer's `endpoints/ray.py`, repointed)

```python
"""Transparent reverse proxy for the Ray Serve status API (`/api/serve/*`), which
the SPA's /serve page reads raw. Mounted at the root (no /api/v1 prefix),
include_in_schema=False. Uses api_route(methods=…) deliberately — this is a
transparent proxy, not an application route."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ray_api.dependencies import HttpDep
from ray_kit import dashboard
from service_kit.dependencies import SettingsDep


router = APIRouter(include_in_schema=False)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


async def _proxy(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
    body = await request.body()
    resp = await dashboard.proxy(
        http,
        settings.ray_dashboard_url,
        path,
        request.method,
        request.url.query,
        dict(request.headers),
        body,
    )
    return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)


def _register_proxy(prefix: str) -> None:
    """Forward `<prefix>` and `<prefix>/{path:path}` to the Ray Dashboard."""
    suffix = prefix.lstrip("/")

    async def catchall(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
        return await _proxy(request, http, settings, f"{suffix}/{path}")

    async def catchall_root(request: Request, http: HttpDep, settings: SettingsDep) -> Response:
        return await _proxy(request, http, settings, suffix)

    router.add_api_route(f"{prefix}/{{path:path}}", catchall, methods=_PROXY_METHODS, name=f"ray-proxy-{prefix}")
    router.add_api_route(prefix, catchall_root, methods=["GET", "HEAD"], name=f"ray-proxy-{prefix}-root")


# Only the Serve status API is proxied — the SPA's /serve page reads it raw.
for _prefix in ("/api/serve",):
    _register_proxy(_prefix)
```
(Note: the original module-global router was named `proxy_router`; here it is `router` in its own module — `_register_proxy` and the bodies reference `router`. Verify the renamed references are consistent.)

- [ ] **Step 5: Create `ray_api/health.py`** (own health router — same as volumes_api/search_api)

```python
from fastapi import APIRouter
from pydantic import BaseModel

from service_kit.dependencies import SettingsDep


class Health(BaseModel):
    status: str
    input: str
    output: str


router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: SettingsDep) -> Health:
    return Health(status="ok", input=settings.viewer_input, output=settings.viewer_output)
```

- [ ] **Step 6: Rewrite `ray_api/__init__.py`**

```python
"""ray-api — Ray dashboard introspection (+ health) and the Ray Serve proxy.
Thin shell over ray-kit; no viewer, no DB. The proxy_router mounts at the root
(no /api/v1 prefix) so /api/serve/* reaches the Ray Serve status API."""

from ray_api import health, proxy, routes
from ray_api.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(
    title="ray-api",
    routers=[health.router, routes.router],
    proxy_router=proxy.router,
    lifespan=make_lifespan,
)
```

- [ ] **Step 7: Update `components/services/ray_api/pyproject.toml`** (drop viewer; add ray-kit + httpx)

Keep `[project]` name/version/license. Set the description to `"ray-api backend — Ray dashboard introspection + Ray Serve proxy over ray-kit."` and set:
```toml
dependencies = [
    "service-kit",
    "ray-kit",
    "httpx>=0.27",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ray_api"]

[tool.uv.sources]
service-kit = { workspace = true }
ray-kit = { workspace = true }
```

- [ ] **Step 8: Update `projects/ray-api/pyproject.toml`** (drop viewer + storage; add ray-kit)

```toml
[project]
name = "ray-api-project"
version = "0.1.0"
description = "Deployable: ray-api service."
requires-python = ">=3.13"
dependencies = ["ray-api"]

[tool.uv.sources]
ray-api = { workspace = true }
ray-kit = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/ray_api",
    "../../packages/ray-kit",
    "../../packages/service-kit",
]
```

- [ ] **Step 9: Remove ray from viewer**

```bash
git rm components/services/viewer/src/viewer/api/v1/endpoints/ray.py
```
`viewer/api/v1/router.py` — remove `ray` from the endpoints import and delete `api_router.include_router(ray.router)`. (The import line becomes `from viewer.api.v1.endpoints import batches, catalog, chunks, health, orchestrator` — no `ray`, no `search` already gone.)

`viewer/main.py` — change `from viewer.api.v1.endpoints import ray, spa` to `from viewer.api.v1.endpoints import spa` and delete the line `app.include_router(ray.proxy_router)` (main.py:91). Grep `main.py` afterwards for any remaining `ray.` reference and remove it.

- [ ] **Step 10: Create `components/services/ray_api/tests/conftest.py`**

```python
"""Test isolation for ray-api. The app singleton bakes Settings at import via
make_service_app → build_settings → load_dotenv (which may read the dev .env).

RAY_DASHBOARD_URL is forced to an unreachable address so build_client returns
None and the dashboard HTTP calls fail fast — the endpoints then exercise their
offline (ok=False) paths deterministically. RASK_API_PREFIX/VIEWER_INPUT/OUTPUT
are defaulted so Settings validates at import; eager import bakes them in."""

import os


os.environ["RAY_DASHBOARD_URL"] = "http://127.0.0.1:9"  # discard/closed port → refused fast
os.environ.setdefault("RASK_API_PREFIX", "/api/v1")
os.environ.setdefault("RASK_VIEWER_INPUT", "/dev/null")
os.environ.setdefault("RASK_VIEWER_OUTPUT", "/dev/null")

import ray_api as _ra  # noqa: F401
```

- [ ] **Step 11: Create `components/services/ray_api/tests/test_ray.py`**

```python
"""ray-api smoke tests — offline (Ray dashboard unreachable per conftest)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    from ray_api import app

    with TestClient(app) as c:
        yield c


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ray_health_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_ray_jobs_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/jobs")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_ray_cluster_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/cluster")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_serve_proxy_unreachable_returns_502(client: TestClient) -> None:
    resp = client.get("/api/serve/applications/")
    assert resp.status_code == 502
```

- [ ] **Step 12: Add the ray-api test dir to root `pyproject.toml` testpaths**

Append `"components/services/ray_api/tests"` to the `testpaths` list.

- [ ] **Step 13: Verify the cut, resolve, test, typecheck, lint**

```bash
grep -rn "import viewer\|from viewer" components/services/ray_api/src packages/ray-kit/src && echo "STILL IMPORTS VIEWER (fail)" || echo "clean — ray-api + ray-kit have no viewer import"
grep -rn "endpoints import ray\|ray.proxy_router\|ray.router" components/services/viewer/src && echo "RESIDUAL viewer ray endpoint (fail)" || echo "clean — viewer no longer serves ray"
uv sync --all-packages
uv tree --package ray-api 2>/dev/null | grep -iwE "viewer|lancedb|sqlmodel|sqlalchemy" && echo "HEAVY DEP LEAKED (fail)" || echo "clean — no viewer/lancedb/sqlmodel in ray-api tree"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import ray_api, sys; assert not any(m=='viewer' or m.startswith('viewer.') for m in sys.modules), 'viewer imported!'; print('ray_api: no viewer in module graph')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import gateway, core_api, search_api, volumes_api, ray_api, orchestrator, viewer.main; print('all services import OK')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/ray-kit packages/service-kit components/services
uvx ruff check packages/ray-kit components/services
```
Expected: "clean — ray-api + ray-kit have no viewer import"; "clean — viewer no longer serves ray"; "clean — no viewer/lancedb/sqlmodel in ray-api tree" (ray IS expected via ray-kit); `ray_api: no viewer in module graph`; `all services import OK`; suite **~111 passed** (106 + 5 new ray-api tests; if a viewer app-smoke test exercised `/api/serve` or `/ray` it may have been removed — investigate any deviation and report); `ty` no new diagnostics; `ruff` clean of new findings (only the 2 pre-existing E501s). If `viewer/main.py` had other `ray` references, fix them; report.

- [ ] **Step 14: Commit**

```bash
git add components/services/ray_api projects/ray-api components/services/viewer pyproject.toml uv.lock
git commit -m "refactor(ray-api): own routes/proxy/lifespan/health over ray-kit; drop the viewer dependency"
```
After committing, run `git status --short` and confirm no `.claude/skills/**` or `components/apps/frontend/**` file was committed.

---

## Task 3: Live verification through the gateway

**Files:** none (runtime check).

- [ ] **Step 1: Restart only the rask fleet (do NOT touch Ray; never `make ray-down`)**

The fleet runs from `./dev-micro.sh` (a process group). Find its group via the gateway port and stop it, then relaunch:
```bash
pid=$(ss -tlnpH "sport = :8888" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
PGID=$(ps -o pgid= -p "${pid:-0}" 2>/dev/null | tr -d ' ')
[ -n "$PGID" ] && kill -TERM "-$PGID"
for i in $(seq 1 25); do busy=""; for p in 8888 8801 8802 8803 8804 8810; do ss -tlnH "sport = :$p" | grep -q . && busy="$busy $p"; done; [ -z "$busy" ] && break; read -t 1 <> <(:) || true; done
ORCH_AUTOSTART=true ./dev-micro.sh > /tmp/rask-fleet.log 2>&1 &
```
(Requires Ray + Postgres already up. `ORCH_AUTOSTART=true` keeps HTR submission running.)

- [ ] **Step 2: Hit ray through the gateway + ray-api's own health**

```bash
curl -s --retry 40 --retry-delay 1 --retry-connrefused --retry-all-errors -o /dev/null \
  -w "gateway /api/ray/health -> %{http_code}\n" "http://127.0.0.1:8888/api/ray/health"
curl -s -o /dev/null -w "gateway /api/ray/jobs    -> %{http_code}\n" "http://127.0.0.1:8888/api/ray/jobs"
curl -s -o /dev/null -w "gateway /api/serve/applications/ -> %{http_code}\n" "http://127.0.0.1:8888/api/serve/applications/"
curl -s -o /dev/null -w "ray-api :8804/api/health -> %{http_code}\n" "http://127.0.0.1:8804/api/health"
curl -s "http://127.0.0.1:8888/api/orchestrator/state" | python3 -c "import sys,json; d=json.load(sys.stdin); print('orchestrator running:', d['running'])"
```
Expected: `/api/ray/health` → `200` (served through gateway → independent ray-api; body `ok:true` if the dev dashboard is reachable, else `ok:false`), `/api/ray/jobs` → `200`, `/api/serve/applications/` → `200` (or `502` if the Serve dashboard is down) — proving the proxy routes via ray-api; `:8804/api/health` → `200`; orchestrator still running (it uses ray-kit now). If the local Ray dashboard is up, `/api/ray/jobs` returns real jobs.

- [ ] **Step 3: Done** — no commit (runtime check only).

---

## Done criteria

- `packages/ray-kit` owns the Ray schemas + dashboard service + `build_client` + `RAY_TRANSIENT_ERRORS`, imports **no** viewer.
- `ray-api` owns routes/proxy/lifespan/health over `ray-kit`, imports **no** viewer; its dep tree has **no** viewer/lancedb/sqlmodel (ray via ray-kit is expected).
- `viewer` imports its Ray bits from `ray-kit`, no longer serves `/ray` or `/api/serve/*`; the orchestrator still runs.
- Full `not slow` suite green; `ty` no new diagnostics; `ruff` clean of new findings; gateway serves ray + the serve-proxy from the independent ray-api.

## Out of scope (next cycle)

Cycle 4 capstone: merge `core-api` + `orchestrator` into one DB-owning service and dissolve the `viewer` package; then the Helm per-service deployment cycle.
