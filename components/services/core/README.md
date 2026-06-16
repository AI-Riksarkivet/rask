# core

FastAPI backend for the rask core. Serves images and ALTO XML from any
`Source` (filesystem, MinIO, HCP), exposes Lance-backed line/catalog search,
proxies the Ray dashboard, and optionally hosts the SvelteKit SPA.

> For the **design rationale + before/after layout**, see
> [`docs/architecture/viewer-backend.md`](../../../docs/architecture/viewer-backend.md).

## Layout

```
src/core/
├── main.py            # create_app() + uvicorn entry
├── api/v1/endpoints/  # one router per concern (health, volumes, batches, chunks, search, catalog, orchestrator, ray, spa)
├── api/dependencies.py
├── core/              # config, db, exceptions, lifespan
├── models/            # SQLModel — Batch + StrEnums
├── repositories/      # async data access
├── schemas/           # Pydantic response models
└── services/          # business logic
```

## Endpoints (high level)

```
GET   /api/health
GET   /api/volumes/{vol}/pages              -> [PageEntry]
GET   /api/volumes/{vol}/pages/{key}/image
GET   /api/volumes/{vol}/pages/{key}/alto

GET   /api/batches | /api/batches/{id} | /api/batches/random | /api/batches/{id}/catalog
POST  /api/batches/sync

GET   /api/chunks
POST  /api/chunks/{id}/submit

GET   /api/search | /api/search/stats | /api/search/thumb/{key}
GET   /api/catalog/search | /api/catalog/search/stats | /api/catalog/browse

GET   /api/orchestrator/state
GET   /api/ray/health | /api/ray/jobs | /api/ray/cluster
ALL   /ray-dashboard/{path}  + /api/v0/*, /api/jobs/*, /logs/*  (Ray iframe proxy)

GET   /api/docs   /api/redoc   /api/openapi.json
```

Volume IDs are key prefixes in the input bucket (e.g. `A0060198/`). The viewer
navigates by **known** volume ID — there is no global inventory listing.

## Config

All settings load from `.env` (or env vars) via `pydantic-settings`. See
`.env.example` for the full list. Key vars:

| Var | Purpose | Default |
|---|---|---|
| `RASK_VIEWER_INPUT` | Source URI (s3:// or filesystem path) | required |
| `RASK_VIEWER_OUTPUT` | ALTO output URI | required |
| `HCP_ENDPOINT` / `HCP_USERNAME` / `HCP_PASSWORD` | S3-compatible store creds | unset → S3 features disabled |
| `RASK_SEARCH_BUCKET` | Lance dataset bucket | `images-batch-search` |
| `RAY_DASHBOARD_URL` | Ray dashboard HTTP base | `http://localhost:8265` |
| `DATABASE_URL` | Async SQLAlchemy URL — sqlite **or** postgres (set `postgresql+asyncpg://…`) | falls back to `sqlite+aiosqlite:///.cache/batches.db` |

## Run

```bash
make viewer              # uvicorn on :8888 with --reload, sqlite default
```

Or directly:

```bash
RASK_VIEWER_INPUT=s3://images-batch RASK_VIEWER_OUTPUT=s3://images-batch-alto \
  uv run uvicorn core.main:app --host 0.0.0.0 --port 8888
```

Or via the FastAPI CLI:

```bash
uv run fastapi run core.main:app --port 8888
```

### Against postgres

```bash
uv pip install -e components/services/core[postgres]    # adds asyncpg
DATABASE_URL=postgresql+asyncpg://user:pass@host/dbname \
  RASK_VIEWER_INPUT=… RASK_VIEWER_OUTPUT=… \
  uv run uvicorn core.main:app
```

Same code path. The repository layer is dialect-agnostic; see the
[architecture doc](../../../docs/architecture/viewer-backend.md) §
"Dialect gotchas" for the portability notes.

## Frontend

The SvelteKit app lives at `components/apps/frontend/`. Dev flow:

```bash
make viewer            # backend on :8888
make viewer-frontend   # vite dev server on :5173, proxies /api → :8888
```

For production, build once and the FastAPI process serves both at `:8888`:

```bash
make viewer-frontend-build
make viewer
```

## Tests

```bash
uv run pytest components/services/core/tests
```
