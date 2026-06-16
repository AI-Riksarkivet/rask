# Extract search-api into a real microservice (strangler cycle 2)

**Date:** 2026-06-16
**Status:** Approved (design), pending implementation plan
**Strategy:** Strangler extraction (approach A, continued from the volumes-api pilot).
This spec covers **only** the `search-api` cut. `ray-api` and the `core-api`+`orchestrator`
DB-ownership decision remain separate follow-up cycles.

## Problem

After cycle 1, `search-api` is still a thin facade: its `__init__.py` imports `viewer`'s
`search`/`health` routers and injects `viewer`'s full `make_lifespan`, which builds **DB +
Lance + Ray + S3 + the orchestrator task** even though search uses none of those except one
Lance table and S3. So `search-api` still depends on the whole `viewer` package (Postgres,
Ray, sqlmodel, every router) — no code ownership, no independent image, no fault isolation.

Goal: sever `search-api` from `viewer` so it owns its code and depends only on
`service-kit` + `storage` + `lancedb`, building **only** the resources search needs.

## Why search is the right next slice

Search is **self-contained and stateful in exactly one dimension** (the Lance lines table):

- **Zero database coupling** — search endpoints never touch Postgres/the ORM.
- **No Ray, no orchestrator** — search submits nothing, reads no batch state.
- **Self-contained service + schemas** — `search` service touches Lance only through an
  injected `AsyncTable` handle; its schemas are search-exclusive.
- **Offline-tolerant** — every search function returns gracefully when the table is `None`.

The only new thing this cycle introduces over volumes-api is **a per-service lifespan that
opens one Lance table + an S3 client** and exposes them on `app.state`.

## Why catalog stays in core-api (not moved here)

`catalog` *looks* like search (FTS over a Lance table) but is **not** separable into search-api:
it overlays Postgres `batches` status on results (`batch_repo.count_at_tier` /
`browse_at_tier`, `local_batch_status`) and uses a **different** Lance table (`catalog_tbl`,
not `lines_tbl`). It is correctly owned by `core-api` (which already has the DB). Search and
catalog share no service code and no table, so cutting search out leaves catalog untouched.

## Design

### 1. What `search-api` owns (severed from `viewer`)

```
components/services/search_api/src/search_api/
  __init__.py        # make_service_app(title="search-api",
                     #   routers=[health.router, routes.router], lifespan=make_lifespan)
  lifespan.py        # make_lifespan(settings): open lines table + S3 → app.state
  dependencies.py    # get_lines_tbl, get_s3 (read app.state) + LinesTblDep / S3Dep
  routes.py          # ← moved from viewer/api/v1/endpoints/search.py (imports repointed)
  service.py         # ← moved from viewer/services/discover/search.py (imports repointed)
  schemas.py         # ← moved from viewer/schemas/search.py (LineRow, SearchHit,
                     #   SearchResponse, SearchStats); drop the unused SearchHit.catalog field
  health.py          # own health router (same pattern as volumes_api/health.py)
components/services/search_api/tests/test_search.py   # new — fake-table-driven smoke tests
```

(Flat layout — `routes.py` / `service.py` / `schemas.py` / `health.py` — matching the
already-extracted `volumes_api` brick, so the two services stay structurally consistent.)

`search-api`'s routes are unchanged: `GET /search/`, `GET /search/stats`,
`GET /search/thumb/{thumb_path:path}` (mounted under `settings.api_prefix`).

### 2. The one new abstraction — `search_api/lifespan.py`

A `make_lifespan(settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]` factory
(the injectable lifespan type added in cycle 1, accepted by `make_service_app(lifespan=...)`).
On startup it builds **only**:

- `app.state.settings = settings`
- `app.state.s3` — the S3 client (`storage.s3_client(settings)` / the same call viewer uses)
- `app.state.lines_tbl` — `await lancedb.connect_async(settings.lance_db_uri,
  storage_options=…)` then open the lines table; **None-tolerant** (returns `None` when HCP
  creds or the table are missing — identical behaviour to today).

It builds **none** of: DB engine/sessionmaker, Ray client, orchestrator task, HTTP client,
`catalog_tbl`. The Lance-open logic is the **lines-only subset** of `viewer`'s
`core/lifespan.py::_open_lancedb`, copied into `search-api` (per the approved
per-service-copy decision — no shared lance helper yet; YAGNI until a second Lance consumer
beyond viewer exists). `viewer` keeps its own `_open_lancedb` (it still opens `catalog_tbl`
for core-api).

### 3. `search_api/dependencies.py`

```python
def get_lines_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.lines_tbl

def get_s3(request: Request) -> S3Client:
    return request.app.state.s3

LinesTblDep = Annotated[AsyncTable | None, Depends(get_lines_tbl)]
S3Dep = Annotated[S3Client, Depends(get_s3)]
```

Imports `from lancedb.table import AsyncTable` and `from storage import S3Client` (these are
why `service-kit` must NOT host them — it stays lancedb-free). `SettingsDep` continues to come
from `service_kit.dependencies`.

### 4. Schemas

Move `viewer/schemas/search.py` → `search_api/schemas.py` verbatim, **dropping the
`SearchHit.catalog: CatalogHit | None` field** and its `from viewer.schemas.catalog import
CatalogHit` import. Verified: the field is never populated by `search_lines` (always `None`),
and removing it severs the last schema link to `viewer`. No response shape that any client
relies on changes (the field only ever serialized as `null`).

### 5. What changes in `viewer`

- **Delete** `api/v1/endpoints/search.py`, `services/discover/search.py`, `schemas/search.py`.
- `api/v1/router.py`: remove `search` from the endpoints import list and drop its
  `include_router(search.router)`.
- **Targeted dead-code cleanup:** with search gone, `viewer`'s `lines_tbl` wiring is orphaned
  (catalog/batches use `catalog_tbl`, not `lines_tbl`). Remove `get_lines_tbl` / `LinesTblDep`
  from `viewer/api/dependencies.py`, and the `app.state.lines_tbl` assignment + the lines-table
  open in `viewer/core/lifespan.py`, so viewer stops opening a table it no longer serves.
  **Guard:** grep the whole `viewer` tree first; only remove if nothing else references
  `lines_tbl` / `get_lines_tbl` / `LinesTblDep`. Behaviour-preserving for every remaining
  viewer endpoint.

### 6. Dependencies

`components/services/search_api/pyproject.toml` deps become **`service-kit`, `storage`,
`lancedb`, `uvicorn`** (drop `viewer`); `[tool.uv.sources]` keeps `service-kit` + `storage`
workspace entries. `projects/search-api/pyproject.toml` drops `viewer` from `[tool.uv.sources]`
and from `[tool.uv.workspace] members` (members become search_api + service-kit + storage).

### 7. End-state dependency graph (after this cycle)

```
storage ── service-kit         (platform: config/exceptions/middleware/factory/get_settings/lifespan)
   │            ↑     ↑
   │      volumes-api  search-api ── lancedb        viewer (+ core-api / ray-api / orchestrator facades)
   │   (service-kit+storage)  (service-kit+storage+lancedb;
   │                           NO viewer, NO DB/ray/sqlmodel)
```

### 8. Testing

- New `components/services/search_api/tests/test_search.py`: endpoint smoke tests via a fake
  `AsyncTable` (reuse the `_FakeLinesTbl` shape already in viewer's tests) — search hit → 200
  with expected `thumb_url`, `/stats`, and a thumb 404. Set env before `from search_api import
  app` (app builds at import time, same gotcha as volumes_api).
- Move the existing `test_search_thumb_url_includes_api_prefix` unit test out of
  `viewer/tests/test_pipelines_registry.py` into the search-api tests.
- Add `components/services/search_api/tests` to root `[tool.pytest.ini_options] testpaths`.
- `viewer` suite stays green minus the moved search test.

## Verification

- `uv sync --all-packages` resolves; `uv tree --package search-api` shows **no** `viewer`, and
  **no** `ray`/`sqlmodel`/`sqlalchemy` (lancedb IS expected — search owns it).
- `grep -rn "import viewer\|from viewer" components/services/search_api/src` → nothing.
- Import-smoke: `import search_api` with no `viewer` in its module graph.
- Full `not slow` suite green (run the project's way — keep `addopts`; never `-o addopts=""`).
- `uvx ty check` adds no new diagnostics; `uvx ruff check packages/service-kit
  components/services` is clean of any **new** findings (the 2 pre-existing E501s in
  `viewer/main.py` / `orchestrator/loop.py` are out of scope).
- Live: restart the fleet (do NOT touch Ray; never `make ray-down`), then
  `curl :8888/api/search/?q=test&limit=1` → 200 via gateway → search-api, and
  `:8802/api/health` → 200 (search-api's own health router).

## Non-goals (this cycle)

- No change to `core-api` (keeps `catalog`/`batches`/`chunks`), `ray-api`, or `orchestrator`.
- No change to runtime behaviour of the search endpoints.
- **No Helm/k8s work.** This cycle delivers the *code/dependency* isolation that makes a
  separate `search-api` pod possible; authoring per-service Deployment/Service templates and
  wiring the gateway's `RASK_*_API_URL` to cluster DNS is a separate, application-code-free
  deployment cycle.

## Follow-up cycles

1. Extract `ray-api` (owns the Ray dashboard/proxy + a Ray resource builder).
2. **Decide DB ownership for `core-api` + `orchestrator`** (own design pass).
3. **Helm per-service deployment** — Deployment+Service per service, gateway env-routing,
   split images — the cycle that turns code isolation into real separate pods.
