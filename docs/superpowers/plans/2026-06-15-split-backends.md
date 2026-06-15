# Split Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single `components/services/backends` brick into one brick + one deployable per service (`gateway`, `core_api`, `search_api`, `volumes_api`, `ray_api`, `orchestrator`), with the shared `_common` factory promoted to `packages/service-kit`.

**Architecture:** Pure packaging refactor — no route handler or runtime behaviour changes. The six service modules become six one-file packages, each exposing `app`. New bricks are added to the uv workspace alongside the old `backends` brick (both resolve fine), then the old brick is deleted last so the workspace stays green at every commit.

**Tech Stack:** Python 3.13, uv workspace, hatchling build backend, FastAPI, `ty` type-checker.

---

## Notes for the implementer

- **This is a refactor with zero behaviour change.** There are no new unit tests to write. Each task's verification is: the workspace resolves (`uv sync --all-packages`), the app(s) import, and `ty` passes. The final task also smoke-tests the dev fleet.
- **Always run from the repo root** `/home/morgan/rask`.
- **Ray/uv gotcha:** import smoke checks use `uv run --no-sync` and must export `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (viewer imports `ray`).
- **`ty` runs with `error-on-warning = true`** — warnings fail.
- The five viewer-based services import their route handlers from `viewer.api.v1.endpoints` (unchanged). Only the factory import path changes: `backends._common` → `service_kit`.
- The old `backends` brick stays in place and untouched until Task 9; the duplicate factory it carries is harmless (no name collision with the new packages).
- **No `Makefile` change is needed.** Its only `backends` references are descriptive comments ("gateway + per-domain backends") that remain accurate; the `dev-micro` target shells out to `dev-micro.sh`, which Task 8 updates.

## File structure (what each new file owns)

```
packages/service-kit/
  pyproject.toml                       # distribution "service-kit"; deps viewer, storage, fastapi, python-dotenv
  src/service_kit/__init__.py          # make_service_app, build_settings, _setup_logging (verbatim from _common.py)

components/services/gateway/
  pyproject.toml                       # distribution "gateway"; deps fastapi, httpx, uvicorn, python-dotenv (NO workspace deps)
  src/gateway/__init__.py              # app (HTTP proxy) — verbatim from gateway.py, logger renamed
components/services/core_api/
  pyproject.toml                       # distribution "core-api"; deps service-kit, viewer, uvicorn
  src/core_api/__init__.py             # app
components/services/search_api/
  pyproject.toml                       # distribution "search-api"
  src/search_api/__init__.py           # app
components/services/volumes_api/
  pyproject.toml                       # distribution "volumes-api"
  src/volumes_api/__init__.py          # app
components/services/ray_api/
  pyproject.toml                       # distribution "ray-api"
  src/ray_api/__init__.py              # app
components/services/orchestrator/
  pyproject.toml                       # distribution "orchestrator"
  src/orchestrator/__init__.py         # app

projects/gateway/pyproject.toml        # deployable composition (mini-workspace)
projects/core-api/pyproject.toml
projects/search-api/pyproject.toml
projects/volumes-api/pyproject.toml
projects/ray-api/pyproject.toml
projects/orchestrator/pyproject.toml
```

Deleted in Task 9: `components/services/backends/`, `projects/backends/`.

---

## Task 1: Promote `_common` to `packages/service-kit`

**Files:**
- Create: `packages/service-kit/pyproject.toml`
- Create: `packages/service-kit/src/service_kit/__init__.py`
- Modify: `pyproject.toml` (root) — `[tool.uv.workspace] members`

- [ ] **Step 1: Create the package manifest**

Create `packages/service-kit/pyproject.toml`:

```toml
[project]
name = "service-kit"
version = "0.1.0"
description = "Shared FastAPI app factory for rask backend services (lifespan, config, logging, middleware)."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "viewer",
    "storage",
    "fastapi>=0.115",
    "python-dotenv>=1.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/service_kit"]

[tool.uv.sources]
viewer = { workspace = true }
storage = { workspace = true }
```

- [ ] **Step 2: Create the module**

Copy the current body of `components/services/backends/src/backends/_common.py` into
`packages/service-kit/src/service_kit/__init__.py` **verbatim** (all imports, `_setup_logging`,
`build_settings`, `make_service_app`). Only change the module docstring's first line to:

```python
"""Shared factory for rask backend services (was backends/_common.py)."""
```

Leave everything else — including the `("viewer", "backends")` logger list in `_setup_logging` — unchanged, to guarantee zero behaviour change.

- [ ] **Step 3: Register in the root workspace**

In root `pyproject.toml`, under `[tool.uv.workspace] members`, add `"packages/service-kit",`
after `"packages/storage",`. Do NOT remove `"components/services/backends"` yet.

- [ ] **Step 4: Resolve the workspace**

Run: `uv sync --all-packages`
Expected: completes without error; `service-kit` appears in the install set.

- [ ] **Step 5: Import-smoke the factory**

Run: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import service_kit; assert service_kit.make_service_app"`
Expected: exits 0, no traceback.

- [ ] **Step 6: Type-check**

Run: `uvx ty check packages/service-kit`
Expected: PASS (no errors).

- [ ] **Step 7: Commit**

```bash
git add packages/service-kit pyproject.toml
git commit -m "refactor(backends): promote _common to packages/service-kit"
```

---

## Task 2: Create the `gateway` brick + deployable

**Files:**
- Create: `components/services/gateway/pyproject.toml`
- Create: `components/services/gateway/src/gateway/__init__.py`
- Create: `projects/gateway/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/gateway/pyproject.toml`:

```toml
[project]
name = "gateway"
version = "0.1.0"
description = "API gateway — path-routes /api/* to the per-domain backends."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "fastapi>=0.115",
    "httpx>=0.27",
    "uvicorn>=0.30",
    "python-dotenv>=1.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/gateway"]
```

- [ ] **Step 2: Create the app module**

Copy `components/services/backends/src/backends/gateway.py` **verbatim** into
`components/services/gateway/src/gateway/__init__.py`, changing only the logger name:

```python
log = logging.getLogger("gateway")
```

(was `logging.getLogger("backends.gateway")`). Everything else — routing table, `_merged_openapi`,
`lifespan`, `proxy` — is unchanged.

- [ ] **Step 3: Create the deployable composition**

Create `projects/gateway/pyproject.toml`:

```toml
[project]
name = "gateway-project"
version = "0.1.0"
description = "Deployable: API gateway."
requires-python = ">=3.13"
dependencies = ["gateway"]

[tool.uv.sources]
gateway = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/gateway",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/gateway",`
after `"components/services/viewer",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import gateway; assert gateway.app"
uvx ty check components/services/gateway
```
Expected: all three succeed, no traceback, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/gateway projects/gateway pyproject.toml
git commit -m "refactor(backends): extract gateway into its own brick + deployable"
```

---

## Task 3: Create the `core_api` brick + deployable

**Files:**
- Create: `components/services/core_api/pyproject.toml`
- Create: `components/services/core_api/src/core_api/__init__.py`
- Create: `projects/core-api/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/core_api/pyproject.toml`:

```toml
[project]
name = "core-api"
version = "0.1.0"
description = "core-api backend — batches/chunks/catalog over the viewer routers."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "viewer",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/core_api"]

[tool.uv.sources]
service-kit = { workspace = true }
viewer = { workspace = true }
```

- [ ] **Step 2: Create the app module**

Create `components/services/core_api/src/core_api/__init__.py`:

```python
"""core-api — the state-mutating core: batches, chunks, catalog (+ health).

Owns the Postgres `batches` table and Ray submit for chunk ops. Runs WITHOUT the
orchestrator loop (that is its own process); ensure `RASK_ORCHESTRATOR_AUTOSTART`
is unset/0 for this service.
"""

from service_kit import make_service_app
from viewer.api.v1.endpoints import batches, catalog, chunks, health


app = make_service_app(
    title="core-api",
    routers=[health.router, batches.router, chunks.router, catalog.router],
)
```

- [ ] **Step 3: Create the deployable composition**

Create `projects/core-api/pyproject.toml`:

```toml
[project]
name = "core-api-project"
version = "0.1.0"
description = "Deployable: core-api service."
requires-python = ">=3.13"
dependencies = ["core-api"]

[tool.uv.sources]
core-api = { workspace = true }
viewer = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/core_api",
    "../../components/services/viewer",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/core_api",`
after `"components/services/gateway",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import core_api; assert core_api.app"
uvx ty check components/services/core_api
```
Expected: all succeed, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/core_api projects/core-api pyproject.toml
git commit -m "refactor(backends): extract core_api into its own brick + deployable"
```

---

## Task 4: Create the `search_api` brick + deployable

**Files:**
- Create: `components/services/search_api/pyproject.toml`
- Create: `components/services/search_api/src/search_api/__init__.py`
- Create: `projects/search-api/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/search_api/pyproject.toml`:

```toml
[project]
name = "search-api"
version = "0.1.0"
description = "search-api backend — line-level FTS + thumbnails over the viewer routers."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "viewer",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/search_api"]

[tool.uv.sources]
service-kit = { workspace = true }
viewer = { workspace = true }
```

- [ ] **Step 2: Create the app module**

Create `components/services/search_api/src/search_api/__init__.py`:

```python
"""search-api — line-level FTS + thumbnails (+ health). LanceDB + S3, no DB."""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, search


app = make_service_app(title="search-api", routers=[health.router, search.router])
```

- [ ] **Step 3: Create the deployable composition**

Create `projects/search-api/pyproject.toml`:

```toml
[project]
name = "search-api-project"
version = "0.1.0"
description = "Deployable: search-api service."
requires-python = ">=3.13"
dependencies = ["search-api"]

[tool.uv.sources]
search-api = { workspace = true }
viewer = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/search_api",
    "../../components/services/viewer",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/search_api",`
after `"components/services/core_api",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import search_api; assert search_api.app"
uvx ty check components/services/search_api
```
Expected: all succeed, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/search_api projects/search-api pyproject.toml
git commit -m "refactor(backends): extract search_api into its own brick + deployable"
```

---

## Task 5: Create the `volumes_api` brick + deployable

**Files:**
- Create: `components/services/volumes_api/pyproject.toml`
- Create: `components/services/volumes_api/src/volumes_api/__init__.py`
- Create: `projects/volumes-api/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/volumes_api/pyproject.toml`:

```toml
[project]
name = "volumes-api"
version = "0.1.0"
description = "volumes-api backend — image + ALTO serving over S3/IIIF, over the viewer routers."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "viewer",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/volumes_api"]

[tool.uv.sources]
service-kit = { workspace = true }
viewer = { workspace = true }
```

- [ ] **Step 2: Create the app module**

Create `components/services/volumes_api/src/volumes_api/__init__.py`:

```python
"""volumes-api — image + ALTO serving over S3/IIIF (+ health). No DB."""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, volumes


app = make_service_app(title="volumes-api", routers=[health.router, volumes.router])
```

- [ ] **Step 3: Create the deployable composition**

Create `projects/volumes-api/pyproject.toml`:

```toml
[project]
name = "volumes-api-project"
version = "0.1.0"
description = "Deployable: volumes-api service."
requires-python = ">=3.13"
dependencies = ["volumes-api"]

[tool.uv.sources]
volumes-api = { workspace = true }
viewer = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/volumes_api",
    "../../components/services/viewer",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/volumes_api",`
after `"components/services/search_api",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import volumes_api; assert volumes_api.app"
uvx ty check components/services/volumes_api
```
Expected: all succeed, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/volumes_api projects/volumes-api pyproject.toml
git commit -m "refactor(backends): extract volumes_api into its own brick + deployable"
```

---

## Task 6: Create the `ray_api` brick + deployable

**Files:**
- Create: `components/services/ray_api/pyproject.toml`
- Create: `components/services/ray_api/src/ray_api/__init__.py`
- Create: `projects/ray-api/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/ray_api/pyproject.toml`:

```toml
[project]
name = "ray-api"
version = "0.1.0"
description = "ray-api backend — Ray dashboard introspection + Ray Serve proxy, over the viewer routers."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "viewer",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ray_api"]

[tool.uv.sources]
service-kit = { workspace = true }
viewer = { workspace = true }
```

- [ ] **Step 2: Create the app module**

Create `components/services/ray_api/src/ray_api/__init__.py`:

```python
"""ray-api — Ray dashboard introspection (+ health) and the Ray Serve proxy.

The `proxy_router` is mounted at the root (no `/api/v1` prefix), exactly as in
`viewer.main`, so `/api/serve/*` reaches the Ray Serve status API.
"""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, ray


app = make_service_app(
    title="ray-api",
    routers=[health.router, ray.router],
    proxy_router=ray.proxy_router,
)
```

- [ ] **Step 3: Create the deployable composition**

Create `projects/ray-api/pyproject.toml`:

```toml
[project]
name = "ray-api-project"
version = "0.1.0"
description = "Deployable: ray-api service."
requires-python = ">=3.13"
dependencies = ["ray-api"]

[tool.uv.sources]
ray-api = { workspace = true }
viewer = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/ray_api",
    "../../components/services/viewer",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/ray_api",`
after `"components/services/volumes_api",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import ray_api; assert ray_api.app"
uvx ty check components/services/ray_api
```
Expected: all succeed, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/ray_api projects/ray-api pyproject.toml
git commit -m "refactor(backends): extract ray_api into its own brick + deployable"
```

---

## Task 7: Create the `orchestrator` brick + deployable

**Files:**
- Create: `components/services/orchestrator/pyproject.toml`
- Create: `components/services/orchestrator/src/orchestrator/__init__.py`
- Create: `projects/orchestrator/pyproject.toml`
- Modify: `pyproject.toml` (root) — workspace members

- [ ] **Step 1: Create the brick manifest**

Create `components/services/orchestrator/pyproject.toml`:

```toml
[project]
name = "orchestrator"
version = "0.1.0"
description = "orchestrator backend — the reconcile→derive→submit loop as its own process."
requires-python = ">=3.13"
license = "AGPL-3.0-only"
dependencies = [
    "service-kit",
    "viewer",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/orchestrator"]

[tool.uv.sources]
service-kit = { workspace = true }
viewer = { workspace = true }
```

- [ ] **Step 2: Create the app module**

Create `components/services/orchestrator/src/orchestrator/__init__.py`:

```python
"""orchestrator — the reconcile→derive→submit loop as its own process.

For the local trial this stays the in-process timer loop: `make_lifespan` starts
it when `RASK_ORCHESTRATOR_AUTOSTART=1` (set in `Procfile.micro`). Only `/health`
is exposed. The eventual production form is a NATS JetStream consumer.
"""

from service_kit import make_service_app
from viewer.api.v1.endpoints import health, orchestrator


# Serves /api/orchestrator/{state,start,stop} so this process's own loop can be
# controlled at runtime (start/stop flip app.state.orchestrator_task here).
app = make_service_app(title="orchestrator", routers=[health.router, orchestrator.router])
```

- [ ] **Step 3: Create the deployable composition**

Create `projects/orchestrator/pyproject.toml`:

```toml
[project]
name = "orchestrator-project"
version = "0.1.0"
description = "Deployable: orchestrator service."
requires-python = ">=3.13"
dependencies = ["orchestrator"]

[tool.uv.sources]
orchestrator = { workspace = true }
viewer = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/orchestrator",
    "../../components/services/viewer",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 4: Register the brick in the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, add `"components/services/orchestrator",`
after `"components/services/ray_api",`.

- [ ] **Step 5: Resolve + import-smoke + type-check**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import orchestrator; assert orchestrator.app"
uvx ty check components/services/orchestrator
```
Expected: all succeed, ty PASS.

- [ ] **Step 6: Commit**

```bash
git add components/services/orchestrator projects/orchestrator pyproject.toml
git commit -m "refactor(backends): extract orchestrator into its own brick + deployable"
```

---

## Task 8: Point `dev-micro.sh` at the new module paths

**Files:**
- Modify: `dev-micro.sh` (the six `run` lines)

- [ ] **Step 1: Update the run lines**

In `dev-micro.sh`, replace the six `run` invocations with (only the module path changes —
`backends.<mod>:app` → `<mod>:app`):

```bash
run gateway     "$GATEWAY_PORT" gateway:app
run core-api    "$CORE_PORT"    core_api:app    env RASK_ORCHESTRATOR_AUTOSTART=false
run search-api  "$SEARCH_PORT"  search_api:app  env RASK_ORCHESTRATOR_AUTOSTART=false
run volumes-api "$VOLUMES_PORT" volumes_api:app env RASK_ORCHESTRATOR_AUTOSTART=false
run ray-api     "$RAY_PORT"     ray_api:app     env RASK_ORCHESTRATOR_AUTOSTART=false
run orchestrator "$ORCH_PORT"   orchestrator:app env RASK_ORCHESTRATOR_AUTOSTART="$ORCH_AUTOSTART"
```

- [ ] **Step 2: Verify each new module path is importable as uvicorn would load it**

Run:
```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import gateway, core_api, search_api, volumes_api, ray_api, orchestrator; print('all import OK')"
```
Expected: prints `all import OK`, no traceback.

- [ ] **Step 3: Commit**

```bash
git add dev-micro.sh
git commit -m "refactor(backends): point dev-micro.sh at per-service module paths"
```

---

## Task 9: Delete the old `backends` brick + deployable

**Files:**
- Delete: `components/services/backends/`
- Delete: `projects/backends/`
- Modify: `pyproject.toml` (root) — remove the `backends` workspace member

- [ ] **Step 1: Remove the old brick from the root workspace**

In root `pyproject.toml` `[tool.uv.workspace] members`, delete the line
`"components/services/backends",`.

- [ ] **Step 2: Delete the directories**

```bash
git rm -r components/services/backends projects/backends
```

- [ ] **Step 3: Full workspace resolve**

Run: `uv sync --all-packages`
Expected: completes; no reference to `backends` distribution remains.

- [ ] **Step 4: Import-smoke every new service**

Run:
```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 uv run --no-sync python -c "import gateway, core_api, search_api, volumes_api, ray_api, orchestrator; print('all import OK')"
```
Expected: prints `all import OK`.

- [ ] **Step 5: Type-check the whole tree**

Run: `uvx ty check`
Expected: PASS (no errors, no warnings).

- [ ] **Step 6: Smoke-test the dev fleet (requires `make ray-up` + `make pg-up` first if not already running)**

Run: `make dev-micro`
Expected: logs `fleet up — gateway on http://127.0.0.1:8888`. In another shell:
`curl -s http://127.0.0.1:8888/api/v1/docs | grep -q "rask API" && echo DOCS_OK`
Expected: prints `DOCS_OK`. Then Ctrl-C to stop the fleet.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(backends): remove old monolithic backends brick + deployable"
```

---

## Done criteria

- `components/services/{gateway,core_api,search_api,volumes_api,ray_api,orchestrator}` each exist as standalone bricks with their own `pyproject.toml`.
- `packages/service-kit` holds the shared factory; the five viewer-based bricks depend on it.
- `projects/{gateway,core-api,search-api,volumes-api,ray-api,orchestrator}` each compose one deployable.
- `components/services/backends` and `projects/backends` are gone.
- `uv sync --all-packages`, `uvx ty check`, and `make dev-micro` all pass.
- No route handler or runtime behaviour changed.

## Out of scope (separate follow-up plan)

Helm chart Deployments/Services per backend + gateway, and the `values.yaml` backends section,
with `gateway` as the `:8888` origin. Tracked separately; coordinate with the in-flight `chart/` work.
```
