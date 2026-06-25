# core

The rask **core domain brick** (the dissolved `viewer`; package `core`). Owns the
batches/chunks/catalog/orchestrator endpoints, the DB + Alembic, and the domain
services. Exposes Lance-backed **catalog** search and the SvelteKit SPA fallback.
Not a deployable on its own — composed by two thin entrypoints (`core-api` :8801
with the orchestrator loop OFF, `orchestrator` :8810 with it ON); `main.py` is the
monolith factory, still used by tests + `make viewer`. Image/ALTO serving, **line**
search, and the Ray dashboard proxy now live in the separate `volumes-api`,
`search-api`, and `ray-api` services.

> For the **design rationale + before/after layout**, see
> [`docs/architecture/viewer-backend.md`](../../../docs/architecture/viewer-backend.md).

## Layout

```
src/core/
├── main.py            # create_app() + uvicorn entry
├── api/v1/endpoints/  # one router per concern (health, batches, chunks, catalog, orchestrator, spa)
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

GET   /api/batches | /api/batches/{id} | /api/batches/random | /api/batches/{id}/catalog
POST  /api/batches/{id}/register | /api/batches/{id}/upload | /api/batches/sync

GET   /api/chunks
POST  /api/chunks/{id}/submit | /api/chunks/{id}/stop

GET   /api/catalog/search | /api/catalog/search/stats | /api/catalog/browse

GET   /api/orchestrator/state
POST  /api/orchestrator/start | /api/orchestrator/stop

GET   /api/docs   /api/redoc   /api/openapi.json
```

Image/ALTO serving (`/api/volumes/*`), line search (`/api/search/*`), and the Ray
dashboard proxy (`/api/ray/*`, `/ray-dashboard/*`) are **not** here — they live in
the standalone `volumes-api`, `search-api`, and `ray-api` services.

## Config

All settings load from `.env` (or env vars) via `pydantic-settings`. See
`.env.example` for the full list. Key vars:

| Var | Purpose | Default |
|---|---|---|
| `RASK_VIEWER_INPUT` | Source URI (s3:// or filesystem path) | required |
| `RASK_VIEWER_OUTPUT` | ALTO output URI | required |
| `RASK_S3_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | S3 backend (MinIO/rustfs/AWS) creds | unset → S3 features disabled |
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

The SvelteKit catch-all app lives at `components/frontends/home/`; the rest are the
per-domain microfrontends under `components/frontends/`. Dev flow:

```bash
make viewer            # core monolith on :8888 (dev convenience)
make home   # catch-all vite dev server on :5173, proxies /api → :8888
make dev-frontends     # all 7 apps behind the :3024 microfrontends proxy
```

The `spa.py` fallback (mounted only when a static build dir exists) is a dev/legacy
convenience. In production the frontend is built into per-domain SSR images and
composed by the k3s Ingress — FastAPI does **not** serve the SPA. Build all apps
with `make frontend-build`.

## Tests

```bash
uv run pytest components/services/core/tests
```
