# Services

`components/services/` holds the **viewer** — the only HTTP backend in rask.

## Viewer — `components/services/viewer`

A FastAPI service on port **8888**, serving `/api/v1/*` to the SPA. It has **no
authentication** and only minimal middleware (optional CORS, request-id, timing);
it assumes a trusted/localhost network. It owns `alembic/`, the `Batch` model,
and the in-process orchestrator loop. See [Projects → Viewer](../projects/viewer.md)
for the full configuration and lifecycle, and the
[API reference](../reference/viewer.md) for symbol-level docs.

### Endpoint groups

All paths are under `RASK_API_PREFIX` (default `/api/v1`), except the Ray
dashboard proxy which sits at the root under `/api/serve/*`.

| Group | Routes (selected) |
|---|---|
| health | `GET /health` |
| volumes | `GET /volumes/{vol}/pages`, `…/pages/{key}/image`, `…/pages/{key}/alto` |
| batches | `GET /batches/`, `/batches/{id}`, `/batches/{id}/catalog`, `GET /batches/random`, `POST /batches/sync` |
| chunks | `GET /chunks/`, `POST /chunks/{id}/submit`, `POST /chunks/{id}/stop` |
| search | `GET /search/`, `/search/stats`, `/search/thumb/{path}` |
| catalog | `GET /catalog/search`, `/catalog/search/stats`, `/catalog/browse` |
| orchestrator | `GET /orchestrator/state`, `POST /orchestrator/start`, `/stop` |
| ray | `GET /ray/health`, `/ray/jobs`, `/ray/jobs/{id}/logs`, `/ray/cluster`, `/ray/actors`, `/ray/tasks`, `/ray/overview`, `/ray/logs` + `/api/serve/*` proxy |

### Services layer

- `services/sync.py` — `reconcile_from_s3`: count cached/transcribed pages per
  batch and update `htr_status`. Idempotent; powers `POST /batches/sync` and the
  orchestrator.
- `services/submission.py` — `submit_chunk` / `stop_chunk`; `build_entrypoint`
  picks `uv run … runner` (runner specs) vs. `python … htr_chunk_job.py` (http
  specs). Submission IDs are `<pipeline>-chunk-NNN-of-MMM-<timestamp>`.
- `services/orchestrator/` — `loop.py` (the tick/run_loop task) and `derive.py`
  (`derive_state`: classify Ray jobs into prefetch/HTR lanes, compute eligible
  chunks excluding in-flight + cooled-down).
- `services/discover/` — `search.py` (line FTS) and `catalog.py` (EAD FTS +
  browse) over Lance tables.
- `services/ray_dashboard.py` — the `JobSubmissionClient` wrapper plus
  `cluster_status` / `list_jobs` / `list_actors` / `list_tasks` / `proxy`.

### Models & state

- `models/batch.py` — the `Batch` SQLModel; enums (`HtrStatus`,
  `ManifestStatus`) stored as lowercase strings via `SAEnum(values_callable=…)`.
- `models/pipelines.py` — `PipelineSpec` + `PIPELINE_SPECS` (`htr`, `htrflow`,
  `htr_http`, `prefetch`, `fake`). Keys match the runner's `--pipeline` names.
- ORM is **SQLModel + SQLAlchemy async** — SQLite in dev, Postgres in prod via
  `DATABASE_URL`; schema changes go through **Alembic** (never `create_all`).

!!! note "The orchestrator is in-process"
    The submission loop is a lifespan-managed `asyncio.Task`, not a cron or
    queue — it lives and dies with the viewer process. It is explicitly
    transitional (a NATS JetStream consumer is the intended successor).
