# Extract volumes-api into a real microservice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sever `volumes-api` from `viewer` so it owns its code and depends only on `service-kit` + `storage`, by first inverting `viewer.core`'s platform infra into `service-kit` and making the lifespan injectable.

**Architecture:** Strangler extraction. `service-kit` becomes the shared platform library (config, exceptions, middleware, app factory, generic `get_settings`, an injectable default lifespan). `viewer` and the four remaining facade bricks keep working by importing the platform from `service-kit` (via thin re-export shims) and passing `viewer`'s full `make_lifespan` into the factory. `volumes-api` owns its routes/service/schema, passes no lifespan, and drops its `viewer` dependency.

**Tech Stack:** Python 3.13, uv workspace, hatchling, FastAPI, pydantic-settings, `ty`, pytest (importlib mode).

---

## Notes for the implementer

- Run from repo root `/home/morgan/rask`. Branch is already `refactor/extract-volumes-api`.
- **Ray/uv gotcha:** any `uv run` importing `viewer`/`ray` needs `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and `--no-sync`. App imports need `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` set (Settings validates at import).
- **Never run pytest with `-o addopts=""`** — it drops `--import-mode=importlib` and causes spurious `ModuleNotFoundError: No module named 'tests.X'` collection errors. Quiet runs: `... pytest -m "not slow" -p no:cacheprovider --no-header -q`.
- Commit rules for EVERY commit: no `Co-Authored-By`, no Claude/AI mention, exact message given, do not push.
- This is a behaviour-preserving refactor. The existing `viewer` test suite is the safety net for Tasks 1–3; Task 4 moves the volumes test to the new brick.
- Baseline: full `not slow` suite is green on this branch before starting (verify once: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q` → `98 passed`).

## File structure (end state of this plan)

```
packages/service-kit/src/service_kit/
  __init__.py        # make_service_app(lifespan=...), build_settings, _setup_logging, default_lifespan
  config.py          # ← moved from viewer/core/config.py  (Settings)
  exceptions.py      # ← moved from viewer/core/exceptions.py
  middleware.py      # ← moved from viewer/core/middleware.py
  dependencies.py    # get_settings + SettingsDep (light only)

components/services/viewer/src/viewer/
  core/config.py     # re-export shim: from service_kit.config import *  (+ explicit names)
  core/exceptions.py # re-export shim
  core/middleware.py # re-export shim
  core/lifespan.py   # unchanged logic; imports Settings from service_kit (via shim)
  api/dependencies.py# resource deps stay; get_settings/SettingsDep re-exported from service_kit
  api/v1/router.py   # volumes.router removed
  # endpoints/volumes.py, services/volumes.py, schemas/page.py  → DELETED (moved)

components/services/volumes_api/src/volumes_api/
  __init__.py        # app = make_service_app(title="volumes-api", routers=[routes.router])
  routes.py          # ← endpoints/volumes.py
  service.py         # ← services/volumes.py
  schemas.py         # ← schemas/page.py (PageEntry)
components/services/volumes_api/tests/test_volumes.py   # new
```

---

## Task 1: Make the lifespan injectable in `service-kit`

Goal: `make_service_app` stops hard-wiring `viewer`'s `make_lifespan`; callers inject a lifespan factory, defaulting to a minimal one that only sets `app.state.settings`.

**Files:**
- Modify: `packages/service-kit/src/service_kit/__init__.py`
- Modify: `components/services/{core_api,search_api,volumes_api,ray_api,orchestrator}/src/<pkg>/__init__.py`

- [ ] **Step 1: Add a default lifespan + `lifespan` param to `make_service_app`**

In `packages/service-kit/src/service_kit/__init__.py`, add these imports near the top:

```python
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
```

Add a default lifespan factory above `make_service_app`:

```python
def default_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Minimal lifespan for stateless services: expose `settings` on `app.state`.

    Services that need resources (DB, Lance, Ray, S3) inject their own factory.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        yield

    return lifespan
```

Change the `make_service_app` signature and the `FastAPI(...)` lifespan line:

```python
def make_service_app(
    *,
    title: str,
    routers: Sequence[APIRouter],
    proxy_router: APIRouter | None = None,
    lifespan: Callable[[Settings], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    _setup_logging()
    settings = build_settings()

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=(lifespan or default_lifespan)(settings),
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    ...
```

Delete the now-unused `from viewer.core.lifespan import make_lifespan` import line.

- [ ] **Step 2: Have the four DB/resource facades inject `viewer`'s lifespan**

In each of `core_api`, `search_api`, `ray_api`, `orchestrator` `src/<pkg>/__init__.py`, add the import and pass `lifespan=make_lifespan`. Example for `core_api/__init__.py` — change the imports and the call:

```python
from service_kit import make_service_app
from viewer.api.v1.endpoints import batches, catalog, chunks, health
from viewer.core.lifespan import make_lifespan


app = make_service_app(
    title="core-api",
    routers=[health.router, batches.router, chunks.router, catalog.router],
    lifespan=make_lifespan,
)
```

Apply the same `from viewer.core.lifespan import make_lifespan` + `lifespan=make_lifespan` to `search_api`, `ray_api` (keep its `proxy_router=ray.proxy_router`), and `orchestrator`.

For `volumes_api/__init__.py` **also add `lifespan=make_lifespan` for now** (no behaviour change yet; it's severed in Task 4):

```python
from viewer.core.lifespan import make_lifespan

app = make_service_app(title="volumes-api", routers=[volumes.router], lifespan=make_lifespan)
```

- [ ] **Step 3: Resolve + import-smoke all six services**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import gateway, core_api, search_api, volumes_api, ray_api, orchestrator; print('all import OK')"
```
Expected: `all import OK`.

- [ ] **Step 4: Full suite + typecheck**

```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services
```
Expected: `98 passed`; `ty` adds no new diagnostics.

- [ ] **Step 5: Commit**

```bash
git add packages/service-kit components/services
git commit -m "refactor(service-kit): make the app lifespan injectable; facades pass viewer's make_lifespan"
```

---

## Task 2: Move `config` / `exceptions` / `middleware` into `service-kit`

Goal: `service-kit` owns the platform infra and no longer imports `viewer`; `viewer.core.*` become re-export shims so all existing imports keep working.

**Files:**
- Create: `packages/service-kit/src/service_kit/{config,exceptions,middleware}.py`
- Modify (→ shims): `components/services/viewer/src/viewer/core/{config,exceptions,middleware}.py`
- Modify: `packages/service-kit/src/service_kit/__init__.py`, `packages/service-kit/pyproject.toml`

- [ ] **Step 1: Move the three files into service-kit**

```bash
git mv components/services/viewer/src/viewer/core/config.py     packages/service-kit/src/service_kit/config.py
git mv components/services/viewer/src/viewer/core/exceptions.py packages/service-kit/src/service_kit/exceptions.py
git mv components/services/viewer/src/viewer/core/middleware.py packages/service-kit/src/service_kit/middleware.py
```

In `service_kit/middleware.py`, change its one internal import `from viewer.core.config import Settings` → `from service_kit.config import Settings`. (`config.py` and `exceptions.py` have no viewer imports — leave them.)

- [ ] **Step 2: Recreate `viewer/core/{config,exceptions,middleware}.py` as re-export shims**

`components/services/viewer/src/viewer/core/config.py`:
```python
"""Moved to service_kit.config. Re-exported here so existing `viewer.core.config`
imports keep working during the microservices extraction."""

from service_kit.config import *  # noqa: F401,F403
from service_kit.config import Settings  # noqa: F401
```

`components/services/viewer/src/viewer/core/exceptions.py`:
```python
"""Moved to service_kit.exceptions. Re-exported for back-compat."""

from service_kit.exceptions import *  # noqa: F401,F403
from service_kit.exceptions import (  # noqa: F401
    DomainError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
    register_handlers,
)
```

`components/services/viewer/src/viewer/core/middleware.py`:
```python
"""Moved to service_kit.middleware. Re-exported for back-compat."""

from service_kit.middleware import *  # noqa: F401,F403
from service_kit.middleware import register_middleware  # noqa: F401
```

- [ ] **Step 3: Repoint `service_kit/__init__.py` to its own modules**

Change its imports from:
```python
from viewer.core.config import Settings
from viewer.core.exceptions import register_handlers
from viewer.core.middleware import register_middleware
```
to:
```python
from service_kit.config import Settings
from service_kit.exceptions import register_handlers
from service_kit.middleware import register_middleware
```
Update the module docstring's first line to drop the "imports viewer" claim (now only `viewer`'s *lifespan* is injected by callers).

- [ ] **Step 4: Update `service-kit` deps — drop `viewer`, add pydantic-settings**

Edit `packages/service-kit/pyproject.toml`: in `dependencies`, remove `"viewer"`, and set the list to:
```toml
dependencies = [
    "storage",
    "fastapi>=0.115",
    "pydantic>=2.7",
    "pydantic-settings>=2.5",
    "python-dotenv>=1.0",
]
```
In `[tool.uv.sources]`, remove the `viewer = { workspace = true }` line (keep `storage`).

- [ ] **Step 5: Verify service-kit no longer imports viewer, then resolve + test**

```bash
grep -rn "import viewer\|from viewer" packages/service-kit/src && echo "STILL IMPORTS VIEWER (fail)" || echo "clean — no viewer imports"
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import service_kit, gateway, core_api, search_api, volumes_api, ray_api, orchestrator; print('all import OK')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services
```
Expected: "clean — no viewer imports"; `all import OK`; `98 passed`; `ty` adds no new diagnostics.

- [ ] **Step 6: Commit**

```bash
git add packages/service-kit components/services/viewer
git commit -m "refactor(service-kit): own config/exceptions/middleware; viewer.core re-exports them"
```

---

## Task 3: Provide `get_settings` / `SettingsDep` from `service-kit`

Goal: the light-weight settings dependency lives in `service-kit` (so `volumes-api` can use it without `viewer`'s kitchen-sink `dependencies.py` that imports lancedb/ray/sqlmodel).

**Files:**
- Create: `packages/service-kit/src/service_kit/dependencies.py`
- Modify: `components/services/viewer/src/viewer/api/dependencies.py`

- [ ] **Step 1: Create `service_kit/dependencies.py`**

```python
"""Light DI types shared across services. Resource-specific deps (S3, Lance,
Ray, DB session) stay with their owning services — keep this module free of
lancedb/ray/sqlmodel imports so dependents don't inherit those."""

from typing import Annotated

from fastapi import Depends, Request

from service_kit.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
```

- [ ] **Step 2: Repoint viewer's `get_settings`/`SettingsDep` to service-kit**

In `components/services/viewer/src/viewer/api/dependencies.py`, remove the local `def get_settings(...)` and any local `SettingsDep = Annotated[...]`, and add near the top:

```python
from service_kit.dependencies import SettingsDep, get_settings  # noqa: F401  (re-exported for viewer endpoints)
```

Keep all the resource deps (`get_http`, `get_s3`, `get_lines_tbl`, `get_catalog_tbl`, `get_ray_client`, `get_session`) and their `*Dep` aliases exactly as-is.

- [ ] **Step 3: Resolve + test**

```bash
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services/viewer
```
Expected: `98 passed`; `ty` clean of new diagnostics.

- [ ] **Step 4: Commit**

```bash
git add packages/service-kit components/services/viewer
git commit -m "refactor(service-kit): provide get_settings/SettingsDep; viewer re-exports them"
```

---

## Task 4: Extract `volumes-api` — own its slice, drop `viewer`

Goal: move volumes' routes/service/schema into the brick, sever the `viewer` dependency, and move its test.

**Files:**
- Create: `components/services/volumes_api/src/volumes_api/{routes,service,schemas}.py`
- Modify: `components/services/volumes_api/src/volumes_api/__init__.py`, `pyproject.toml`
- Modify: `projects/volumes-api/pyproject.toml`
- Delete: `viewer/api/v1/endpoints/volumes.py`, `viewer/services/volumes.py`, `viewer/schemas/page.py`
- Modify: `viewer/api/v1/router.py`, `viewer/tests/test_app_smoke.py`
- Create: `components/services/volumes_api/tests/test_volumes.py`

- [ ] **Step 1: Create `volumes_api/schemas.py`**

```python
from pydantic import BaseModel


class PageEntry(BaseModel):
    key: str
    hasAlto: bool
```

- [ ] **Step 2: Create `volumes_api/service.py`** (moved from `viewer/services/volumes.py`, imports repointed)

Copy `viewer/services/volumes.py` verbatim, changing only its imports:
```python
from storage import build_source
from service_kit.config import Settings
from service_kit.exceptions import NotFoundError, ValidationError
from volumes_api.schemas import PageEntry
```
(Everything else — `image_mime`, `_require_under_volume`, `list_pages`, `read_image`, `read_alto` — unchanged.)

- [ ] **Step 3: Create `volumes_api/routes.py`** (moved from `viewer/api/v1/endpoints/volumes.py`, imports repointed)

Copy `viewer/api/v1/endpoints/volumes.py` verbatim, changing only its imports:
```python
from fastapi import APIRouter
from fastapi.responses import Response

from service_kit.dependencies import SettingsDep
from volumes_api.schemas import PageEntry
from volumes_api import service as volumes_service
```
(The three route functions unchanged. Note: `volumes_service.image_mime`, `.list_pages`, `.read_image`, `.read_alto` names are preserved.)

- [ ] **Step 4: Rewrite `volumes_api/__init__.py` — no viewer, no injected lifespan**

```python
"""volumes-api — image + ALTO serving over S3/IIIF (+ health). Stateless: builds
storage sources on demand from settings; no DB/Lance/Ray, no viewer dependency."""

from service_kit import make_service_app
from volumes_api import routes


app = make_service_app(title="volumes-api", routers=[routes.router])
```

- [ ] **Step 5: Update brick + project manifests to drop `viewer`**

`components/services/volumes_api/pyproject.toml` — set deps and sources to (declare `storage` explicitly since `service.py` imports it directly):
```toml
dependencies = [
    "service-kit",
    "storage",
    "uvicorn>=0.30",
]

[tool.hatch.build.targets.wheel]
packages = ["src/volumes_api"]

[tool.uv.sources]
service-kit = { workspace = true }
storage = { workspace = true }
```

`projects/volumes-api/pyproject.toml` — set to:
```toml
[project]
name = "volumes-api-project"
version = "0.1.0"
description = "Deployable: volumes-api service."
requires-python = ">=3.13"
dependencies = ["volumes-api"]

[tool.uv.sources]
volumes-api = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/volumes_api",
    "../../packages/service-kit",
    "../../packages/storage",
]
```

- [ ] **Step 6: Remove volumes from viewer**

```bash
git rm components/services/viewer/src/viewer/api/v1/endpoints/volumes.py \
       components/services/viewer/src/viewer/services/volumes.py \
       components/services/viewer/src/viewer/schemas/page.py
```
In `components/services/viewer/src/viewer/api/v1/router.py`, delete the line `api_router.include_router(volumes.router)` and remove `volumes` from the `from viewer.api.v1.endpoints import (...)` import list.

- [ ] **Step 7: Move the viewer volumes smoke test into volumes-api**

Create `components/services/volumes_api/tests/test_volumes.py`:
```python
"""volumes-api endpoint smoke tests — FS-backed, no DB/Lance/Ray."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from volumes_api import app

    with TestClient(app) as c:
        yield c


def test_list_pages_empty_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_image_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/VOL/missing.jpg/image")
    assert resp.status_code == 404


def test_image_path_outside_volume_returns_400(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/OTHER/x.jpg/image")
    assert resp.status_code == 400
```

Note: `volumes_api/__init__.py` builds `app` at import time, so the fixture sets env **before** `from volumes_api import app`. Because import is cached, run this test module in its own process or ensure the env is acceptable on first import — keep the import inside the fixture as shown (first test to import wins; the values above are valid for all three tests).

In `components/services/viewer/tests/test_app_smoke.py`, delete the `test_list_pages_endpoint_returns_empty` test (it hit `/api/v1/volumes/...`, which viewer no longer serves). If that leaves the file with no tests, replace the body with a viewer-owned smoke test:
```python
def test_health_endpoint_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
```
(Reuse the file's existing `client` fixture.)

- [ ] **Step 8: Add the volumes-api test dir to pytest testpaths**

In root `pyproject.toml` `[tool.pytest.ini_options] testpaths`, add `"components/services/volumes_api/tests"`.

- [ ] **Step 9: Verify the cut — no viewer/heavy deps in volumes-api**

```bash
uv sync --all-packages
grep -rn "import viewer\|from viewer" components/services/volumes_api/src && echo "STILL IMPORTS VIEWER (fail)" || echo "clean — volumes-api has no viewer import"
uv tree --package volumes-api 2>/dev/null | grep -iE "viewer|lancedb|ray|sqlmodel|sqlalchemy" && echo "HEAVY DEP LEAKED (fail)" || echo "clean — no viewer/lancedb/ray/sqlmodel in volumes-api tree"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import volumes_api; assert volumes_api.app; print('volumes_api OK')"
```
Expected: "clean — volumes-api has no viewer import"; "clean — no viewer/lancedb/ray/sqlmodel in volumes-api tree"; `volumes_api OK`.

- [ ] **Step 10: Full suite + typecheck**

```bash
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services
uvx ruff check components/services/volumes_api packages/service-kit
```
Expected: suite green (count = previous 98 minus 1 moved viewer test plus 3 new volumes-api tests = `100 passed`, adjust if your baseline differs); `ty`/`ruff` clean.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor(volumes-api): own routes/service/schema; drop the viewer dependency"
```

---

## Task 5: Live verification through the gateway

**Files:** none (runtime check).

- [ ] **Step 1: Restart only the rask fleet (do NOT touch Ray — and never `make ray-down`, it is host-wide)**

If a fleet is running, stop it by killing the uvicorn master on each fleet port (kill by port — do NOT `pkill -f 'dev-micro'`, whose pattern matches the killing shell's own command line and self-kills the shell), then relaunch:
```bash
for p in 8888 8801 8802 8803 8804 8810; do
  pid=$(ss -tlnpH "sport = :$p" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$pid" ] && kill -TERM "$pid"
done
sleep 2
ORCH_AUTOSTART=false ./dev-micro.sh > /tmp/rask-fleet.log 2>&1 &
```
(Requires Ray + Postgres already up. Do not run `make ray-down`/`ray stop` — host-wide, kills the Gemma LLM cluster too.)

- [ ] **Step 2: Hit volumes through the gateway**

```bash
# wait for :8888 then:
curl -s -o /dev/null -w "gateway /api/volumes health path -> %{http_code}\n" "http://127.0.0.1:8888/api/volumes/NONEXISTENT/pages"
```
Expected: `200` (empty list for an unknown volume) — proving the gateway routes `/api/volumes/*` to the now-independent `volumes-api`. (Prefix is `/api` from local `.env`.)

- [ ] **Step 3: Done** — no commit (runtime check only).

---

## Done criteria

- `service-kit` owns `config`/`exceptions`/`middleware`/`get_settings`/`make_service_app`/`default_lifespan`, imports **no** `viewer`.
- `viewer.core.{config,exceptions,middleware}` are thin re-export shims; the four facades still work by injecting `viewer`'s `make_lifespan`.
- `volumes-api` owns `routes`/`service`/`schemas`, depends on `service-kit` (+ `storage`) only, and its dependency tree contains **no** `viewer`/`lancedb`/`ray`/`sqlmodel`.
- Full `not slow` suite green; `ty`/`ruff` clean; gateway serves volumes from the independent service.

## Out of scope (follow-up cycles)

Extract `search_api` (Lance resource builder), `ray_api` (Ray resource builder), then the `core_api`+`orchestrator` shared-Postgres ownership decision (its own design pass).
