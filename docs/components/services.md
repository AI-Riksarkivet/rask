# Services

`components/services/` holds the HTTP backend fleet — a **gateway** reverse
proxy plus five per-domain services, all built over a shared **core** brick.
The old monolithic `viewer` service was dissolved (June 2026) into this layout.

## Gateway — `components/services/gateway`

App `gateway:app`, port **:8888**. The frontend's single proxy target and the
only external-facing service. Receives `/api/*` and routes by longest-prefix to
the services below; owns no state and no DB. Upstream URLs are env-overridable:

| Env var | Default | Upstream |
|---|---|---|
| `RASK_CORE_API_URL` | `http://localhost:8801` | core-api |
| `RASK_SEARCH_API_URL` | `http://localhost:8802` | search-api |
| `RASK_VOLUMES_API_URL` | `http://localhost:8803` | volumes-api |
| `RASK_RAY_API_URL` | `http://localhost:8804` | ray-api |
| `RASK_ORCH_API_URL` | `http://localhost:8810` | orchestrator |

## Core brick — `components/services/core`

Package `core`. **Not a deployable on its own** — composed by `core-api` and
`orchestrator`, which run as two processes over the same brick so they share the
`batches` table transactionally.

Owns:

- **DB** — `core/db.py`, `core/lifespan.py`, Alembic migrations in
  `components/services/core/alembic/`.
- **Models** — `models/{batch,enums,pipelines}`. `Batch` SQLModel; enums
  (`HtrStatus`, `ManifestStatus`) stored as lowercase strings via
  `SAEnum(values_callable=…)`. `PipelineSpec` + `PIPELINE_SPECS` (`htr`,
  `htrflow`, `htr_http`, `prefetch`, `fake`).
- **Repositories** — `repositories/batch`.
- **Domain services:**
  - `services/sync.py` — `reconcile_from_s3`: count cached/transcribed pages per
    batch, update `htr_status`. Idempotent; powers `POST /batches/sync` and the
    orchestrator.
  - `services/submission.py` — `submit_chunk` / `stop_chunk`; `build_entrypoint`
    picks `uv run … runner` (runner specs) vs. `python … htr_chunk_job.py`
    (http specs). Submission IDs are `<pipeline>-chunk-NNN-of-MMM-<timestamp>`.
  - `services/orchestrator/loop.py` — the tick/`run_loop` task (transitional →
    NATS).
  - `services/orchestrator/derive.py` — `derive_state`: classify Ray jobs into
    prefetch/HTR lanes, compute eligible chunks excluding in-flight +
    cooled-down.
  - `services/discover/catalog.py` — EAD FTS + browse over Lance tables.
- **Endpoints** — health / batches / chunks / catalog / orchestrator.
- **`main.py`** — monolith app factory used by `make viewer` (single-process dev
  convenience) and the test suite.

ORM is **SQLModel + SQLAlchemy async** — SQLite in dev (`.cache/batches.db`),
Postgres in prod via `DATABASE_URL`. Schema changes go through **Alembic**
(never `create_all`).

```bash
make pg-migrate   # uv run --package core alembic upgrade head
```

## core-api — `components/services/core_api`

App `core_api:app`, port **:8801**. Thin entrypoint over core: health +
batches + chunks + catalog endpoints. Orchestrator loop **off**. Exposes no
state of its own.

## orchestrator — `components/services/orchestrator`

App `orchestrator:app`, port **:8810**. Thin entrypoint over core: health +
orchestrator endpoints. Orchestrator lifespan loop **on**
(`RASK_ORCHESTRATOR_AUTOSTART`). Toggle at runtime via
`POST /api/v1/orchestrator/start` and `/stop`; inspect with
`GET /api/v1/orchestrator/state`.

!!! note "Two processes, one brick"
    `core-api` (loop OFF) and `orchestrator` (loop ON) are deliberately split
    so the submission loop runs in exactly one process. They share the same
    `batches` table and the same `core` source tree. The loop is explicitly
    transitional — the intended successor is a NATS JetStream consumer.

## volumes-api — `components/services/volumes_api`

Port **:8803**. Independent, stateless S3/IIIF image + ALTO proxy. No DB, no
`core` dependency. Deps: `service-kit` + `storage`.

Endpoint groups:

| Group | Routes |
|---|---|
| health | `GET /health` |
| volumes | `GET /volumes/{vol}/pages`, `…/pages/{key}/image`, `…/pages/{key}/alto` |

## search-api — `components/services/search_api`

Port **:8802**. Lance `lines` FTS + S3 thumbnails. Owns a `lines`-only
lifespan (opens Lance tables on startup). No DB, no `core` dependency. Deps:
`service-kit` + `storage` + `lancedb`.

Endpoint groups:

| Group | Routes |
|---|---|
| health | `GET /health` |
| search | `GET /search/`, `/search/stats`, `/search/thumb/{path}` |

## ray-api — `components/services/ray_api`

Port **:8804**. Ray dashboard introspection (`/api/ray/*`) + the
`/api/serve/*` proxy. Thin shell over `ray-kit`. No DB, no `core` dependency.
Deps: `service-kit` + `ray-kit` + `httpx`.

Endpoint groups:

| Group | Routes |
|---|---|
| health | `GET /health` |
| ray | `GET /ray/health`, `/ray/jobs`, `/ray/jobs/{id}/logs`, `/ray/cluster`, `/ray/actors`, `/ray/tasks`, `/ray/overview`, `/ray/logs` |
| serve proxy | `/api/serve/*` passthrough |

## Endpoint summary

All paths are under `RASK_API_PREFIX` (default `/api/v1`), then routed through
the gateway. The `/api/serve/*` proxy (for the Ray Serve management API) is
served by `ray-api` at the root level.

| Group | Service | Selected routes |
|---|---|---|
| health | all services | `GET /health` |
| batches | core-api | `GET /batches/`, `/batches/{id}`, `/batches/{id}/catalog`, `GET /batches/random`, `POST /batches/sync` |
| chunks | core-api | `GET /chunks/`, `POST /chunks/{id}/submit`, `POST /chunks/{id}/stop` |
| catalog | core-api | `GET /catalog/search`, `/catalog/search/stats`, `/catalog/browse` |
| volumes | volumes-api | `GET /volumes/{vol}/pages`, `…/pages/{key}/image`, `…/pages/{key}/alto` |
| search | search-api | `GET /search/`, `/search/stats`, `/search/thumb/{path}` |
| orchestrator | orchestrator | `GET /orchestrator/state`, `POST /orchestrator/start`, `/stop` |
| ray | ray-api | `GET /ray/health`, `/ray/jobs`, `/ray/jobs/{id}/logs`, `/ray/cluster`, `/ray/actors`, `/ray/tasks`, `/ray/overview`, `/ray/logs` + `/api/serve/*` proxy |
