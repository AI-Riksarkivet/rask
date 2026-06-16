# Extract search-api into a real microservice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sever `search-api` from `viewer` so it owns its routes/service/schemas/health, builds **only** the Lance lines table + S3 in its own lifespan, and depends on `service-kit` + `storage` + `lancedb` only (no `viewer`, no DB/Ray/sqlmodel).

**Architecture:** Strangler extraction cycle 2, mirroring the volumes-api cut. `service-kit` (the shared platform library) and the cycle-1 injectable lifespan are already in place. `search-api` gets its own brick code + a lines-only Lance lifespan; `viewer` loses the search endpoint and its now-orphaned `lines_tbl` wiring. `catalog` is untouched (stays in `core-api`; it needs Postgres + a different Lance table).

**Tech Stack:** Python 3.13, uv workspace, hatchling, FastAPI, LanceDB (async), pydantic, `ty`, pytest (importlib mode).

---

## Notes for the implementer

- Run from repo root `/home/morgan/rask`. Branch is already `refactor/extract-search-api`.
- **Ray/uv gotcha:** any `uv run` importing `viewer`/`ray` needs `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` and `--no-sync`. App imports need `RASK_VIEWER_INPUT`/`RASK_VIEWER_OUTPUT` set (Settings validates at import).
- **Never run pytest with `-o addopts=""`** — it drops `--import-mode=importlib` and causes spurious `ModuleNotFoundError` collection errors. Quiet runs: `... pytest -m "not slow" -p no:cacheprovider --no-header -q`.
- Commit rules for EVERY commit: no `Co-Authored-By`, no Claude/AI mention, exact message given, do not push.
- Behaviour-preserving refactor. Baseline: full `not slow` suite is green on this branch before starting — **102 passed** (verify once: `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q`).
- **Do NOT stage unrelated files.** `.claude/skills/**` may show as modified in the tree — never `git add -A`; stage explicit paths only.

## File structure (end state)

```
components/services/search_api/src/search_api/
  __init__.py     # make_service_app(title="search-api", routers=[health.router, routes.router], lifespan=make_lifespan)
  lifespan.py     # make_lifespan(settings): open lines table + S3 → app.state  (NEW)
  dependencies.py # get_lines_tbl, get_s3 + LinesTblDep / S3Dep                  (NEW)
  routes.py       # ← viewer/api/v1/endpoints/search.py (imports repointed)
  service.py      # ← viewer/services/discover/search.py (imports repointed)
  schemas.py      # ← viewer/schemas/search.py, minus the unused SearchHit.catalog field
  health.py       # own health router (copy of volumes_api/health.py)            (NEW)
components/services/search_api/tests/test_search.py  # NEW

# removed from viewer in Task 2:
#   api/v1/endpoints/search.py, services/discover/search.py, schemas/search.py  (deleted)
#   api/v1/router.py            (search dropped)
#   api/dependencies.py         (get_lines_tbl / LinesTblDep removed)
#   core/lifespan.py            (lines_tbl wiring removed; catalog_tbl kept)
#   tests/test_pipelines_registry.py (moved search unit test removed)
```

---

## Task 1: Build the independent `search-api` brick (sever search-api → viewer)

Goal: `search-api` owns all its code and stops importing `viewer`. `viewer` is left fully intact in this task (it still serves search) so the suite stays green; viewer is cut in Task 2.

**Files:**
- Create: `components/services/search_api/src/search_api/{schemas,service,routes,health,dependencies,lifespan}.py`
- Modify: `components/services/search_api/src/search_api/__init__.py`
- Modify: `components/services/search_api/pyproject.toml`, `projects/search-api/pyproject.toml`
- Create: `components/services/search_api/tests/test_search.py`
- Modify: root `pyproject.toml` (testpaths)

- [ ] **Step 1: Create `search_api/schemas.py`** (moved from `viewer/schemas/search.py`, dropping the `catalog` field + its viewer import)

```python
from pydantic import BaseModel, field_validator


class LineRow(BaseModel):
    """Lance columns for `lines`. Field order = projection order."""

    batch_id: str
    page_id: str | None = None
    page_idx: int | None = None
    line_id: str | None = None
    line_idx: int | None = None
    text: str
    confidence: float | None = None
    hpos: float | None = None
    vpos: float | None = None
    width: float | None = None
    height: float | None = None
    polygon: list[list[float]] | None = None
    thumb_key: str | None = None

    @field_validator("polygon", mode="before")
    @classmethod
    def _parse_polygon(cls, v: object) -> object:
        """The `lines` index stores polygon as the raw ALTO POINTS string
        ("x,y x,y …"); coerce it to the [[x, y], …] shape the API exposes."""
        if isinstance(v, str):
            if not v.strip():
                return None
            pts: list[list[float]] = []
            for pair in v.split():
                x, _, y = pair.partition(",")
                if y:
                    pts.append([float(x), float(y)])
            return pts or None
        return v


class SearchHit(LineRow):
    thumb_url: str | None = None


class SearchResponse(BaseModel):
    ok: bool
    query: str
    count: int
    hits: list[SearchHit]


class SearchStats(BaseModel):
    available: bool
    rows: int
```

- [ ] **Step 2: Create `search_api/service.py`** (moved from `viewer/services/discover/search.py`, imports repointed — body unchanged)

```python
"""Line-level FTS over the LanceDB table at `s3://<search-bucket>/lines`.

The table handle comes from `LinesTblDep` (opened once in lifespan); this
module is stateless and async-native (LanceDB exposes an async API).
"""

import logging
from datetime import timedelta

from lancedb.table import AsyncTable

from search_api.schemas import LineRow, SearchHit, SearchResponse, SearchStats
from storage import S3Client


log = logging.getLogger(__name__)


_FTS_COLUMN = "text"
_THUMB_KEY_PREFIX = "thumbs/"
_LINE_COLS = list(LineRow.model_fields)


async def search_lines(tbl: AsyncTable | None, query: str, limit: int, timeout: timedelta, *, api_prefix: str) -> SearchResponse:
    if tbl is None:
        return SearchResponse(ok=True, query=query, count=0, hits=[])
    fts = tbl.query().nearest_to_text(query, columns=_FTS_COLUMN)
    rows = await fts.select(_LINE_COLS).limit(limit).to_list(timeout=timeout)
    hits: list[SearchHit] = []
    for row in rows:
        if row.get("thumb_key"):
            row["thumb_url"] = f"{api_prefix}/search/thumb/{row['thumb_key']}"
        hits.append(SearchHit.model_validate(row))
    return SearchResponse(ok=True, query=query, count=len(hits), hits=hits)


async def stats(tbl: AsyncTable | None) -> SearchStats:
    if tbl is None:
        return SearchStats(available=False, rows=0)
    return SearchStats(available=True, rows=await tbl.count_rows())


def fetch_thumb(s3: S3Client, bucket: str, thumb_key: str) -> bytes | None:
    """GET a line thumbnail from the search bucket. Returns bytes or None on miss.

    `thumb_key` MUST start with `thumbs/` — defense in depth so the proxy
    can't be tricked into fetching arbitrary keys.

    The catch is broad because `s3.get_object` raises `botocore.ClientError`
    (NoSuchKey, Forbidden, etc.) and the viewer rule is no reach into
    botocore from above storage. "Any failure → None" matches the intent.
    """
    if not thumb_key.startswith(_THUMB_KEY_PREFIX):
        return None
    try:
        resp = s3.get_object(Bucket=bucket, Key=thumb_key)
        return resp["Body"].read()
    except Exception as exc:
        log.warning(f"thumb GET {bucket}/{thumb_key} failed: {exc}")
        return None
```

- [ ] **Step 3: Create `search_api/dependencies.py`** (the Lance + S3 DI; imports lancedb + storage — this is why service-kit can't host it)

```python
"""search-api DI: the Lance lines table + S3, read from app.state (set in lifespan)."""

from typing import Annotated

from fastapi import Depends, Request
from lancedb.table import AsyncTable

from service_kit.exceptions import ServiceUnavailableError
from storage import S3Client


def get_lines_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.lines_tbl


def get_s3(request: Request) -> S3Client:
    s3 = request.app.state.s3
    if s3 is None:
        raise ServiceUnavailableError("S3 client not configured (HCP_ENDPOINT missing)")
    return s3


LinesTblDep = Annotated[AsyncTable | None, Depends(get_lines_tbl)]
S3Dep = Annotated[S3Client, Depends(get_s3)]
```

- [ ] **Step 4: Create `search_api/lifespan.py`** (the one new abstraction — lines-only subset of viewer's `_open_lancedb` + S3)

```python
"""search-api lifespan — open the Lance lines table + S3 client, expose on app.state.

Stateful in exactly one dimension (the lines table); no DB/Ray/orchestrator.
Tolerant of missing HCP creds / table so tests run offline (lines_tbl = None).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import lancedb
from fastapi import FastAPI
from lancedb.db import AsyncConnection
from lancedb.table import AsyncTable

from service_kit.config import Settings
from storage import S3Client, s3_client


log = logging.getLogger(__name__)


def _build_s3(settings: Settings) -> S3Client | None:
    if not settings.hcp_endpoint:
        return None
    return s3_client(endpoint=settings.hcp_endpoint)


async def _open_lines_table(settings: Settings) -> tuple[AsyncConnection | None, AsyncTable | None]:
    storage_options = settings.lance_storage_options()
    if storage_options is None:
        log.info("lancedb skipped — HCP credentials not configured")
        return None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning(f"could not connect to lancedb at {settings.lance_db_uri}: {exc}")
        return None, None
    try:
        tbl = await db.open_table(settings.lines_table)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning(f"could not open lancedb table {settings.lines_table}: {exc}")
        return db, None
    return db, tbl


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.s3 = _build_s3(settings)
        app.state.lance_db, app.state.lines_tbl = await _open_lines_table(settings)
        log.info("startup_complete")
        try:
            yield
        finally:
            if app.state.lines_tbl is not None:
                app.state.lines_tbl.close()
            if app.state.lance_db is not None:
                app.state.lance_db.close()
            log.info("shutdown_complete")

    return lifespan
```

- [ ] **Step 5: Create `search_api/routes.py`** (moved from `viewer/api/v1/endpoints/search.py`, imports repointed — route bodies unchanged)

```python
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from search_api import service as search_service
from search_api.dependencies import LinesTblDep, S3Dep
from search_api.schemas import SearchResponse, SearchStats
from service_kit.dependencies import SettingsDep
from service_kit.exceptions import NotFoundError


router = APIRouter(prefix="/search", tags=["search"])

_THUMB_CACHE_MAX_AGE_SECS = 24 * 60 * 60  # 24h — line-crop thumbs are immutable per dataset version


@router.get("/")
async def search_lines(
    tbl: LinesTblDep,
    settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> SearchResponse:
    return await search_service.search_lines(tbl, q, limit, timedelta(seconds=settings.lance_query_timeout_seconds), api_prefix=settings.api_prefix)


@router.get("/stats")
async def search_stats(tbl: LinesTblDep) -> SearchStats:
    return await search_service.stats(tbl)


@router.get("/thumb/{thumb_path:path}")
def search_thumb(thumb_path: str, s3: S3Dep, settings: SettingsDep) -> Response:
    data = search_service.fetch_thumb(s3, settings.search_bucket, thumb_path)
    if data is None:
        raise NotFoundError("thumbnail not found")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": f"public, max-age={_THUMB_CACHE_MAX_AGE_SECS}"})
```

- [ ] **Step 6: Create `search_api/health.py`** (own health router — same shape as `volumes_api/health.py`)

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

- [ ] **Step 7: Rewrite `search_api/__init__.py`** (no viewer; inject the new lifespan)

```python
"""search-api — line-level FTS + thumbnails (+ health). LanceDB + S3; no viewer, no DB/Ray."""

from search_api import health, routes
from search_api.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(title="search-api", routers=[health.router, routes.router], lifespan=make_lifespan)
```

- [ ] **Step 8: Update `components/services/search_api/pyproject.toml`** (drop viewer, add lancedb + storage)

Set `dependencies`, the wheel packages, and `[tool.uv.sources]` to:
```toml
dependencies = [
    "service-kit",
    "storage",
    "lancedb>=0.20",
    "uvicorn>=0.30",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/search_api"]

[tool.uv.sources]
service-kit = { workspace = true }
storage = { workspace = true }
```
(Keep the existing `[project]` name/version/license; only change the description to drop "over the viewer routers" — set it to `"search-api backend — line-level FTS + thumbnails over LanceDB."`)

- [ ] **Step 9: Update `projects/search-api/pyproject.toml`** (drop viewer from sources + members)

```toml
[project]
name = "search-api-project"
version = "0.1.0"
description = "Deployable: search-api service."
requires-python = ">=3.13"
dependencies = ["search-api"]

[tool.uv.sources]
search-api = { workspace = true }
storage = { workspace = true }
service-kit = { workspace = true }

[tool.uv.workspace]
members = [
    "../../components/services/search_api",
    "../../packages/storage",
    "../../packages/service-kit",
]
```

- [ ] **Step 10: Create `components/services/search_api/tests/test_search.py`** (offline smoke tests + the moved service unit test)

```python
"""search-api smoke tests — offline (HCP unset → lines_tbl is None), no DB/Ray."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from lancedb.table import AsyncTable


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from search_api import app

    with TestClient(app) as c:
        yield c


def test_search_offline_returns_200_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/search/?q=hej&limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["hits"] == []


def test_stats_offline_returns_unavailable(client: TestClient) -> None:
    resp = client.get("/api/v1/search/stats")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "rows": 0}


def test_thumb_without_s3_returns_503(client: TestClient) -> None:
    resp = client.get("/api/v1/search/thumb/thumbs/VOL/0001.jpg")
    assert resp.status_code == 503


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


class _FakeLinesTbl:
    """Async LanceDB table stand-in: the query() chain returns the seeded rows."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def query(self) -> "_FakeLinesTbl":
        return self

    def nearest_to_text(self, query: str, columns: str) -> "_FakeLinesTbl":
        return self

    def select(self, cols: list[str]) -> "_FakeLinesTbl":
        return self

    def limit(self, n: int) -> "_FakeLinesTbl":
        return self

    async def to_list(self, timeout: timedelta) -> list[dict[str, object]]:
        return self._rows


@pytest.mark.asyncio
async def test_search_thumb_url_includes_api_prefix() -> None:
    """thumb_url must be built from settings.api_prefix (regression: it once
    emitted /api/search/thumb/... with no /v1 so every line thumbnail 404'd)."""
    from search_api import service as search_service

    tbl = _FakeLinesTbl([{"batch_id": "VOL_A", "text": "hej", "thumb_key": "thumbs/VOL_A/0001.jpg"}])
    resp = await search_service.search_lines(cast(AsyncTable, tbl), "hej", 10, timedelta(seconds=5), api_prefix="/api/v1")
    assert resp.hits[0].thumb_url == "/api/v1/search/thumb/thumbs/VOL_A/0001.jpg"
```

- [ ] **Step 11: Add the search-api test dir to root `pyproject.toml` testpaths**

Change the `testpaths` line in `[tool.pytest.ini_options]` to append `"components/services/search_api/tests"`:
```toml
testpaths = ["packages/htr/tests", "packages/storage/tests", "components/services/viewer/tests", "components/apps/runner/tests", "components/services/volumes_api/tests", "components/services/search_api/tests"]
```

- [ ] **Step 12: Verify the cut, resolve, test, typecheck, lint**

```bash
grep -rn "import viewer\|from viewer" components/services/search_api/src && echo "STILL IMPORTS VIEWER (fail)" || echo "clean — search-api has no viewer import"
uv sync --all-packages
uv tree --package search-api 2>/dev/null | grep -iwE "viewer|ray|sqlmodel|sqlalchemy" && echo "HEAVY DEP LEAKED (fail)" || echo "clean — no viewer/ray/sqlmodel in search-api tree"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import search_api; assert search_api.app; import sys; assert not any(m=='viewer' or m.startswith('viewer.') for m in sys.modules), 'viewer imported!'; print('search_api OK, no viewer in module graph')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services
uvx ruff check components/services/search_api packages/service-kit
```
Expected: "clean — search-api has no viewer import"; "clean — no viewer/ray/sqlmodel in search-api tree" (lancedb IS expected); `search_api OK, no viewer in module graph`; suite **~107 passed** (102 baseline + 5 new search-api tests; viewer still has its search test too at this point); `ty` no new diagnostics beyond the known 23 baseline; `ruff` clean. Report actual counts; if the count differs, explain why before proceeding.

- [ ] **Step 13: Commit**

```bash
git add components/services/search_api projects/search-api pyproject.toml uv.lock
git commit -m "refactor(search-api): own routes/service/schema/health + lines lifespan; drop the viewer dependency"
```
After committing, run `git status --short` and confirm no `.claude/skills/**` file was swept in.

---

## Task 2: Remove search from `viewer` + drop the orphaned `lines_tbl` wiring

Goal: delete viewer's now-superseded search code, stop viewer mounting `/search`, and remove the `lines_tbl` resource viewer no longer serves. `catalog_tbl` stays (catalog/batches still use it).

**Pre-checked fact:** `lines_tbl` / `LinesTblDep` / `get_lines_tbl` / `lines_table` are referenced ONLY in `viewer/api/dependencies.py`, `viewer/core/lifespan.py`, and the search files being deleted (verified by grep at plan time). Re-verify in Step 1 before deleting.

**Files:**
- Delete: `viewer/api/v1/endpoints/search.py`, `viewer/services/discover/search.py`, `viewer/schemas/search.py`
- Modify: `viewer/api/v1/router.py`, `viewer/api/dependencies.py`, `viewer/core/lifespan.py`, `viewer/tests/test_pipelines_registry.py`

- [ ] **Step 1: Re-verify nothing but the to-be-deleted code uses `lines_tbl`**

```bash
grep -rn "lines_tbl\|LinesTblDep\|get_lines_tbl\|lines_table" components/services --include=*.py | grep -v "/search_api/"
```
Expected: hits ONLY in `viewer/api/dependencies.py` (lines defining `get_lines_tbl`/`LinesTblDep`), `viewer/core/lifespan.py` (the lines open/assign/close), and `viewer/api/v1/endpoints/search.py` + `viewer/services/discover/search.py` (about to be deleted). If anything ELSE references it, STOP and report — the cleanup assumption is wrong.

- [ ] **Step 2: Delete the three moved viewer files**

```bash
git rm components/services/viewer/src/viewer/api/v1/endpoints/search.py \
       components/services/viewer/src/viewer/services/discover/search.py \
       components/services/viewer/src/viewer/schemas/search.py
```

- [ ] **Step 3: Drop `search` from `viewer/api/v1/router.py`**

Change the import line (remove `search`):
```python
from viewer.api.v1.endpoints import batches, catalog, chunks, health, orchestrator, ray
```
and delete the line:
```python
api_router.include_router(search.router)
```

- [ ] **Step 4: Remove the orphaned `lines_tbl` dep from `viewer/api/dependencies.py`**

Delete the `get_lines_tbl` function:
```python
def get_lines_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.lines_tbl
```
and delete its alias line:
```python
LinesTblDep = Annotated[AsyncTable | None, Depends(get_lines_tbl)]
```
Leave `get_catalog_tbl` / `CatalogTblDep` and everything else. `AsyncTable` is still used by `get_catalog_tbl`, so keep the `from lancedb.table import AsyncTable` import.

- [ ] **Step 5: Remove the `lines_tbl` wiring from `viewer/core/lifespan.py`**

In `_open_lancedb`, stop opening the lines table and return only the catalog table. Replace the body from the `lines = …` line through `return …`:
```python
async def _open_lancedb(
    settings: Settings,
) -> tuple[AsyncConnection | None, AsyncTable | None]:
    storage_options = settings.lance_storage_options()
    if storage_options is None:
        log.info("lancedb skipped — HCP credentials not configured")
        return None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning(f"could not connect to lancedb at {settings.lance_db_uri}: {exc}")
        return None, None
    catalog = await _open_lance_table(db, settings.catalog_table)
    return db, catalog
```
In the `lifespan` body, change the assignment line (drop `lines_tbl`):
```python
        app.state.lance_db, app.state.catalog_tbl = await _open_lancedb(settings)
```
And in the `finally` block, delete the lines-table close:
```python
            if app.state.lines_tbl is not None:
                app.state.lines_tbl.close()
```
(Keep the `catalog_tbl` close and the `lance_db` close.)

- [ ] **Step 6: Remove the moved search unit test from `viewer/tests/test_pipelines_registry.py`**

Delete the `_FakeLinesTbl` class (it now lives in `search_api/tests/test_search.py`) and the `test_search_thumb_url_includes_api_prefix` test, and remove the now-unused import line `from viewer.services.discover import search as search_service`. Verify no other test in that file references `_FakeLinesTbl` or `search_service` (grep before deleting); if one does, leave `_FakeLinesTbl` in place and only remove the test + import.

- [ ] **Step 7: Resolve, test, typecheck, lint**

```bash
grep -rn "lines_tbl\|LinesTblDep\|get_lines_tbl" components/services/viewer && echo "RESIDUAL lines_tbl (fail)" || echo "clean — viewer no longer references lines_tbl"
grep -rn "import viewer\|from viewer" components/services/search_api/src && echo "STILL IMPORTS VIEWER (fail)" || echo "clean — search-api has no viewer import"
uv sync --all-packages
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync python -c "import gateway, core_api, search_api, volumes_api, ray_api, orchestrator; print('all services import OK')"
RAY_ENABLE_UV_RUN_RUNTIME_ENV=0 RASK_VIEWER_INPUT=s3://in RASK_VIEWER_OUTPUT=s3://out uv run --no-sync pytest -m "not slow" -p no:cacheprovider --no-header -q
uvx ty check packages/service-kit components/services
uvx ruff check packages/service-kit components/services
```
Expected: "clean — viewer no longer references lines_tbl"; "clean — search-api has no viewer import"; `all services import OK`; suite **~106 passed** (107 from Task 1 minus the duplicated unit test removed from viewer); `ty` no new diagnostics; `ruff` clean of any NEW findings (the 2 pre-existing `E501`s in `viewer/main.py` and `viewer/services/orchestrator/loop.py` are out of scope — confirm only those two remain, if any). Report actual counts.

- [ ] **Step 8: Commit**

```bash
git add components/services/viewer uv.lock
git commit -m "refactor(viewer): drop search endpoint + orphaned lines_tbl wiring (moved to search-api)"
```
After committing, run `git status --short` and confirm no `.claude/skills/**` or `components/apps/frontend/**` file was swept in.

---

## Task 3: Live verification through the gateway

**Files:** none (runtime check).

- [ ] **Step 1: Restart only the rask fleet (do NOT touch Ray — never `make ray-down`, it is host-wide)**

The fleet runs from `./dev-micro.sh` (a process group). Stop it by killing the group, then relaunch:
```bash
PGID=$(ps -o pgid= -C "dev-micro.sh" 2>/dev/null | tr -d ' ' | head -1)
[ -n "$PGID" ] && kill -TERM "-$PGID"
# wait for fleet ports to free
for i in $(seq 1 20); do busy=""; for p in 8888 8801 8802 8803 8804 8810; do ss -tlnH "sport = :$p" | grep -q . && busy="$busy $p"; done; [ -z "$busy" ] && break; read -t 1 <> <(:) || true; done
ORCH_AUTOSTART=true ./dev-micro.sh > /tmp/rask-fleet.log 2>&1 &
```
(Requires Ray + Postgres already up. `ORCH_AUTOSTART=true` keeps HTR submission running. Never run `make ray-down`/`ray stop` — host-wide, kills the Gemma LLM cluster too.)

- [ ] **Step 2: Hit search through the gateway and search-api's own health**

```bash
curl -s --retry 30 --retry-delay 1 --retry-connrefused --retry-all-errors -o /dev/null \
  -w "gateway /api/search/ -> %{http_code}\n" "http://127.0.0.1:8888/api/search/?q=test&limit=1"
curl -s -o /dev/null -w "gateway /api/search/stats -> %{http_code}\n" "http://127.0.0.1:8888/api/search/stats"
curl -s -o /dev/null -w "search-api :8802/api/health -> %{http_code}\n" "http://127.0.0.1:8802/api/health"
```
Expected: `/api/search/` → `200` (served through gateway → the now-independent search-api; prefix is `/api` from local `.env`), `/api/search/stats` → `200`, `:8802/api/health` → `200`. (If HCP creds are configured in `.env`, search returns real hits; if not, an empty result — both are `200`.)

- [ ] **Step 3: Done** — no commit (runtime check only).

---

## Done criteria

- `search-api` owns `routes`/`service`/`schemas`/`health`/`lifespan`/`dependencies`, imports **no** `viewer`, and its dependency tree contains **no** `viewer`/`ray`/`sqlmodel`/`sqlalchemy` (lancedb is expected).
- `viewer` no longer serves `/search` and no longer opens/holds the `lines_tbl`; `catalog`/`batches` still work via `catalog_tbl`.
- Full `not slow` suite green; `ty` no new diagnostics; `ruff` clean of new findings; gateway serves search from the independent service.

## Out of scope (follow-up cycles)

Extract `ray-api` (Ray resource builder); then the `core-api`+`orchestrator` shared-Postgres ownership decision (its own design pass); then the Helm per-service deployment cycle (Deployment+Service per service, gateway env-routing, split images) that turns this code isolation into real separate pods.
