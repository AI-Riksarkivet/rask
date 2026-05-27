# viewer — backend architecture (post-refactor, 2026-05)

This doc captures the May 2026 refactor of `components/services/viewer/`: what
the service looked like before, what it looks like now, and the design choice
behind each move. The intent is that anyone reading this can answer "why is it
shaped this way?" without reading the diff.

Cross-references: `.claude/skills/fastapi/` (project HTTP conventions),
`.claude/skills/writing-python/` (language-level conventions),
`docs/architecture/system-overview.md` (image → ALTO pipeline).

---

## 1. Before vs after — the file tree

### Before

A single 770-line `app.py` and two sibling modules, all under `src/viewer/`:

```
src/viewer/
├── __init__.py
├── app.py                # 770 lines — every route, every SQL string, every subprocess shell-out, the SPA fallback, the Ray reverse-proxy, custom docs HTML
├── main.py               # argparse + uvicorn.run
├── search.py             # Lance line FTS + EAD catalog FTS mixed
├── ray_dashboard.py      # sync httpx wrapper + hand-rolled JSON mapping
└── static/               # vendored swagger-ui + redoc bundles (~3 MB)
```

All routes lived in `app.py`. Module-level `load_dotenv()` + `derive_hcp_creds()`
ran at import. Every request opened a fresh `httpx.Client`, a fresh
`sqlite3.connect`, a fresh `boto3.client`. SQL strings were inlined into route
handlers. Subprocess calls (`scripts/sync_from_s3.py`, `scripts/submit_chunks.py`)
were invoked via `subprocess.run(..., timeout=600)` from inside HTTP handlers.

### After

Literal `fastapi/references/project-template.md` layout — separated by concern,
~50 lines per file:

```
src/viewer/
├── __init__.py
├── main.py                       # create_app() factory + module-level app + argparse CLI
├── api/
│   ├── __init__.py
│   ├── dependencies.py           # Annotated DI aliases (SettingsDep, HttpDep, S3Dep, …)
│   └── v1/
│       ├── __init__.py
│       ├── router.py             # aggregates endpoints
│       └── endpoints/
│           ├── __init__.py
│           ├── health.py         # /api/health
│           ├── volumes.py        # /api/volumes/{vol}/pages|image|alto
│           ├── batches.py        # /api/batches{,/{id},/random,/sync}
│           ├── chunks.py         # /api/chunks{,/{id}/submit}
│           ├── search.py         # /api/search{,/stats,/thumb/{key}}
│           ├── catalog.py        # /api/catalog/search{,/stats,/browse}, /api/batches/{id}/catalog
│           ├── orchestrator.py   # /api/orchestrator/state
│           ├── ray.py            # /api/ray/{health,jobs,cluster} + reverse-proxy
│           └── spa.py            # SPA fallback (mounted only when build exists)
├── core/
│   ├── __init__.py
│   ├── config.py                 # pydantic-settings
│   ├── db.py                     # AsyncEngine + sessionmaker factory
│   ├── exceptions.py             # DomainError hierarchy + RFC 9457 handlers
│   └── lifespan.py               # @asynccontextmanager — resources on app.state
├── models/
│   ├── __init__.py
│   └── batch.py                  # SQLModel trinity + StrEnums
├── repositories/
│   ├── __init__.py
│   └── batch.py                  # All SQL lives here. Only place that imports `select`.
├── schemas/
│   ├── __init__.py
│   ├── health.py  page.py  batch.py  chunk.py
│   ├── search.py  catalog.py  orchestrator.py  ray.py
├── services/
│   ├── __init__.py
│   ├── batches.py  chunks.py     # ORM-backed
│   ├── catalog.py                # ORM + Lance + bild_id regex guard
│   ├── search.py                 # Lance + S3
│   ├── orchestrator.py           # ORM + Ray SDK + dashboard HTTP for /api/v0
│   ├── ray_dashboard.py          # Ray JobSubmissionClient + cluster/proxy via httpx
│   └── submission.py             # async subprocess wrappers (transitional)
└── tests/
    └── test_app_smoke.py
```

---

## 2. Design choices

Each section: **the move** → **why**.

### 2.1 `pydantic-settings.BaseSettings` for config

> `core/config.py` — `Settings(BaseSettings)` with `env_file=".env"`.

**Before.** Routes called `os.environ["RASK_VIEWER_INPUT"]`,
`os.getenv("HCP_ENDPOINT")`, `os.environ.get("RAY_DASHBOARD_URL", "http://localhost:8265")`
ad hoc. Module-level `load_dotenv()` ran at import.

**Why pydantic-settings.**

- *Fail fast.* Missing required vars raise on `Settings()` construction, not on
  the first request that needs them.
- *One place.* Every env var the service reads is on one class — searchable
  surface area.
- *Type-safe.* `viewer_input: str`, `http_timeout: float`, `db_pool_size: int =
  Field(default=10, ge=1, le=100)` — values come out validated, not as `str`s.
- *Computed properties.* `resolved_database_url` falls back to a sqlite URL
  built from `resolved_batches_db` — the agnosticism is in the type, not in
  every caller.
- *.env native.* Drops `python-dotenv` as a viewer dep; pydantic-settings reads
  `.env` itself.

Skill: `writing-python/References/configuration.md`.

### 2.2 `@asynccontextmanager` lifespan on `app.state`

> `core/lifespan.py:make_lifespan(settings)` builds:
> `httpx.AsyncClient`, `boto3` S3 client (via `storage.s3_client`),
> Lance dataset handles, async DB engine + sessionmaker, Ray
> `JobSubmissionClient`. All disposed cleanly after `yield`.

**Before.** No lifespan. Each request built a new `httpx.Client` (15-line
`with httpx.Client(timeout=...) as c:` block in every Ray-touching handler),
opened a new `sqlite3.connect()` in a `try/finally`, re-opened the Lance dataset
on every `/api/search` request. `boto3.client` was cached via `lru_cache` at
module level. `derive_hcp_creds()` ran at import time as a side effect.

**Why.**

- *Connection reuse.* httpx keeps a pool; boto3 maintains TCP keep-alives; the
  Ray SDK's connection-test runs once. Per-request construction wastes those.
- *Testability.* `create_app()` is a factory the smoke test calls directly;
  `TestClient(app)` triggers the lifespan deterministically.
- *Graceful shutdown.* The lifespan's `finally:` block closes httpx + disposes
  the engine in order — no leaked connections on SIGTERM. uvicorn already
  drains in-flight requests; we don't write custom signal handlers (see
  `fastapi/references/production-patterns.md` § Patterns to avoid).
- *Tolerant init.* HCP creds missing → S3 = None. Ray down → ray_client = None.
  Lance dataset missing → ds = None. Services degrade by returning `ok: false`;
  the app starts.

Skill: `fastapi/references/production-patterns.md` § Lifespan.

### 2.3 `Annotated[T, Depends(...)]` DI types

> `api/dependencies.py` exports `SettingsDep`, `HttpDep`, `S3Dep`, `LinesDsDep`,
> `CatalogDsDep`, `RayClientDep`, `SessionDep`.

**Before.** Routes pulled state via `Path(__file__).resolve().parents[5]`,
`os.environ[...]`, or by directly importing the boto3/Lance/httpx clients.

**Why.**

- *Single source of truth.* Resources live on `app.state` and only DI deps
  pull them out. Tests override via `app.dependency_overrides[get_settings] = …`.
- *Terse signatures.* `def get_batch(batch_id: str, session: SessionDep)`
  reads. Alternative inline `Depends(...)` calls are noisier per route.
- *Type-resolvable.* The `Annotated` form gives ty enough information to
  type-check route bodies. `boto3` lacks first-party stubs, so `S3Dep` uses
  `TYPE_CHECKING` + `mypy_boto3_s3` to keep the type real without a runtime dep.

Skill: `fastapi/references/dependencies.md`.

### 2.4 `DomainError` hierarchy + RFC 9457 Problem Details

> `core/exceptions.py` — `DomainError`, `NotFoundError`, `ValidationError`,
> `ServiceUnavailableError`, `UpstreamUnavailableError`, `UpstreamTimeoutError`.
> Routes `raise NotFoundError(...)`. A single registered handler emits
> `application/problem+json` with `type`, `title`, `status`, `detail`.

**Before.** Routes called `raise HTTPException(503, "batches.db not built")`,
`raise HTTPException(404, f"unknown batch_id: {batch_id}")` ad hoc. No
structured `problem+json`; clients had to parse `{"detail": "..."}` strings.

**Why.**

- *Routes raise domain errors, not HTTP.* The HTTP status is a property of the
  exception class, set in one place. Renaming `NotFoundError` doesn't touch
  the routes.
- *RFC 9457.* `application/problem+json` is the modern HTTP error shape and
  is what every typed-client generator (openapi-typescript, openapi-fetch)
  expects. The frontend gets a stable error envelope to type against.
- *`RequestValidationError` reformatter.* FastAPI's default 422 body is
  verbose and inconsistent with Problem Details. The handler converts it to
  the same shape.
- *Stack traces stay in logs.* `log.exception(...)` in the handler keeps
  trace context attached (OTel-ready). The response carries `title` +
  `detail`, never internals.

Skill: `fastapi/references/exception-handlers.md`.

### 2.5 Routers / services / repositories — strict layering

> `api/v1/endpoints/*.py` — HTTP only. `services/*.py` — business logic.
> `repositories/*.py` — SQL.

**Before.** `app.py` route handlers contained: `httpx.Client(...)` HTTP calls,
`sqlite3.connect(...)` SQL, `subprocess.run(...)` shell-outs, regex parsing,
Pydantic-validation-by-string-checks. One function did all of it.

**Why.**

- *Each layer has one job.*
  - **Repository** imports `select`, knows the SQL dialect, returns ORM rows.
  - **Service** composes multiple repo calls, talks to external systems
    (Lance, Ray, S3), maps ORM → schema. Doesn't import `select`.
  - **Endpoint** parses the HTTP request, calls a service, returns the
    response model. Doesn't import `select`, doesn't open files.
- *Testable.* Repositories can be exercised against an in-memory SQLite.
  Services can be tested with a fixture repository. Endpoints test via
  `TestClient`.
- *Forces honest dependencies.* If a service imports `httpx`, that's a real
  dependency we should think about — not something hiding inside a route
  handler.

Skill: `fastapi/references/project-template.md`.

### 2.6 Pydantic response models on every route

> Every handler has a return type that's a `BaseModel` (or `SQLModel`/
> `Response` for byte streams). No handler returns `-> dict`.

**Before.** Every route returned `-> dict` or `-> list[dict]`. The
`/api/openapi.json` schema was useless — every endpoint advertised
`{additionalProperties: any}`.

**Why.**

- *OpenAPI is the contract.* The frontend generates typed clients from
  `/api/openapi.json`. If the schema says `any`, the client is untyped.
- *Validation at egress.* `response_model` makes FastAPI re-validate the
  outgoing payload — typos like `started_at` vs `start_at` fail at the API
  boundary, not silently.
- *Cheap.* Pydantic v2 is fast; the cost is negligible per request.

Skill: `fastapi/references/core-conventions.md`.

### 2.7 SQLModel "trinity" (`BatchBase` / `Batch` / `BatchPublic`)

> `models/batch.py` defines:
>
> ```python
> class BatchBase(SQLModel): …shared fields with StrEnum types…
> class Batch(BatchBase, table=True): …ORM…
> class BatchPublic(BatchBase): pass
> ```

**Before.** No ORM. `schemas/batch.py` had a Pydantic `Batch(BaseModel)` that
was hand-mapped from `sqlite3.Row` dicts.

**Why trinity (not just one class or two).**

- *API contract ≠ DB schema.* `BatchPublic` is what the frontend sees;
  `Batch` is what's in the table. Adding internal columns (e.g.
  `internal_notes`, `soft_deleted_at`) wouldn't change the API.
- *Scales to writes.* When we eventually accept `POST /api/batches`, a
  `BatchCreate(BatchBase)` subclass slots in without re-declaring fields.
- *Skill-aligned.* This is the canonical FastAPI+SQLModel pattern
  (`fastapi.tiangolo.com/tutorial/sql-databases/`).
- *DRY.* Adding a column means editing `BatchBase` once.

Skill: `fastapi/references/database.md`.

### 2.8 Backend-agnostic DB via `DATABASE_URL`

> `core/db.py:make_engine(settings)` branches on URL scheme.
> `Settings.resolved_database_url` falls back to
> `sqlite+aiosqlite:///{resolved_batches_db}` when `DATABASE_URL` is unset.

**Before.** Routes called `sqlite3.connect(.cache/batches.db)` directly.
Hard-coded path, hard-coded driver, no way to point at postgres.

**Why.**

- *Same code, two backends.* `DATABASE_URL=postgresql+asyncpg://...` swaps
  the driver. Repository methods are SQL-dialect-agnostic (they use
  SQLAlchemy expression API, not raw strings).
- *SQLite dev is honest.* The path is configurable for tests
  (`sqlite+aiosqlite:///:memory:`) without changing service code.
- *Pool flags conditional on scheme.* `pool_pre_ping`, `pool_recycle`,
  `pool_size`, `max_overflow` apply for postgres; sqlite gets defaults
  (single connection, no pool).
- *Alembic-ready.* `SQLModel.metadata.naming_convention` is set on day 1 so
  the day we add migrations, constraint names are stable and rollbacks work.
  Doesn't cost anything until then.

Skill: `fastapi/references/database.md` § Engine, § Migrations.

### 2.9 Ray `JobSubmissionClient` SDK (vs hand-rolled httpx)

> `services/ray_dashboard.py` uses `ray.job_submission.JobSubmissionClient`
> (cached on `app.state.ray_client`) for `health` and `list_jobs`.
> `services/orchestrator.py` uses the SDK's `get_job_info` for the
> driver_job_id lookup. The SDK is sync; calls are wrapped with
> `anyio.to_thread.run_sync` from async handlers.

**Before.** Hand-rolled `httpx.AsyncClient.get(...)` against Ray's HTTP
endpoints, with a `_job_from_raw` mapper, `_ms_to_sec` helper, `_parse_batches`
regex, and a custom `RayJob` schema. ~80 lines of mapping code.

**Why.**

- *Schemas are Ray's, not ours.* `RayJob(JobDetails)` subclasses Ray's
  Pydantic model (`ray.dashboard.modules.job.pydantic_models.JobDetails`) —
  same shape as the official Ray Jobs OpenAPI spec. Version-locked to
  `ray==2.55.1`.
- *Less to maintain.* `JobStatus` and `JobType` are Ray's StrEnums.
  `DriverInfo` is Ray's. We added two derived fields (`batches` parsed from
  entrypoint, `logs_url` deep-link).
- *Connection check at startup.* The SDK constructor verifies the API
  version once during lifespan; subsequent calls reuse the cached
  connection.
- *Cluster_status + proxy stay on httpx.* The SDK doesn't model
  `/api/cluster_status` or generic path forwarding for the iframe; those
  use the shared `httpx.AsyncClient`.

### 2.10 StrEnums everywhere there's a closed string set

> `models/batch.py` — `HtrStatus(StrEnum)`, `ManifestStatus(StrEnum)`,
> `BrowseTier(StrEnum)`. `schemas/ray.py` re-exports Ray's `JobStatus` and
> `JobType` (also StrEnums).

**Before.** `htr_status: str = "pending"`, hand-coded
`if tier not in ("listed", "cached", "transcribed"): raise ValidationError`
in the service.

**Why.**

- *Type-checked.* Passing `"DONE"` (caps mismatch) to a function expecting
  `HtrStatus.DONE` fails at the boundary.
- *FastAPI auto-validates query params.* `tier: BrowseTier =
  BrowseTier.CACHED` lets FastAPI reject `tier=foobar` with a 422 before
  the service runs.
- *Self-documenting OpenAPI.* The schema lists the valid values; the
  frontend can generate a dropdown.

Skill: `fastapi/references/core-conventions.md` § StrEnum.

### 2.11 `Annotated[..., Query(...)]` boundary validation

> Routes use `q: Annotated[str, Query(min_length=1, max_length=500)]`,
> `limit: Annotated[int, Query(ge=1, le=500)] = 50`.

**Before.** Routes did `if limit < 1 or limit > 500: raise HTTPException(400, "limit must be in [1, 500]")` inside the body.

**Why.**

- *Validate at the boundary.* FastAPI rejects bad inputs before the
  handler runs. The service can assume valid arguments.
- *Documented in OpenAPI.* Frontend gets the constraints in the schema;
  it can show "max length 500" in a form.

### 2.12 `storage.s3_client()` — no direct `boto3` import

> Viewer's lifespan calls `storage.s3_client(endpoint=settings.hcp_endpoint)`
> instead of building a client itself. `boto3` is not in viewer's
> `pyproject.toml` deps.

**Before.** Routes (and `search.py`) imported `boto3` directly with their
own `Config(...)` and retry settings.

**Why.**

- *Single wrapper.* The HCP/MinIO setup (path-style addressing, SigV4,
  retry tuning, CA bundle, insecure flag) lives in `packages/storage`
  once. Drift between callers is impossible.
- *Honest deps.* `boto3` is a transitive of `storage`; making it explicit
  in viewer would double-declare.
- *Saved as a project memory* (`feedback-storage-not-boto3.md`).

### 2.13 Dropped vendored static docs bundles

> Removed `src/viewer/static/` (3 MB of `swagger-ui-bundle.js`,
> `redoc.standalone.js`, `swagger-ui.css`) and `endpoints/docs.py`
> (custom HTML routes). FastAPI's defaults at `/api/docs` and `/api/redoc`
> serve Swagger UI from `cdn.jsdelivr.net`.

**Why.**

- *Default works.* For local dev and the deployment topology in scope, no
  CSP injection blocks `cdn.jsdelivr.net`.
- *If CSP becomes a problem later*, re-vendoring is one router + one
  static mount. Until then it's dead code.

### 2.14 Async subprocess (not `subprocess.run`)

> `services/submission.py` wraps `asyncio.create_subprocess_exec` +
> `asyncio.wait_for`. Timeout raises `UpstreamTimeoutError` (504).

**Before.** `subprocess.run(..., timeout=600)` inside an HTTP handler —
the worker thread blocked for up to 10 minutes per request.

**Why.**

- *Doesn't block the event loop.* The async handler stays cooperative.
- *Right HTTP status.* `UpstreamTimeoutError` maps to 504, not the
  ad-hoc 500 the old code returned.
- *Known transitional.* The fact that we're shelling out to scripts at
  all is the next refactor; this just stops it from being a foot-gun
  while it's still there.

### 2.15 Lance filter strings — regex guard at the boundary

> `services/catalog.py:_BILD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")`.
> All `bild_id` inputs to Lance scanner filters validate against this
> first; the f-string interpolation is provably safe.

**Before.** `bild_id.replace("'", "''")` hand-rolled SQL-style escaping
inside `f"bild_id = '{safe_id}'"`. Lance has no parameterized-filter
API, so the only safety was a single regex substitution.

**Why.**

- *Whitelist > escape.* Archive codes are alphanumerics, dashes,
  underscores, dots. Anything else is invalid by definition —
  rejecting at the boundary removes the injection surface, not just
  patches it.

### 2.16 Lance scanner calls via `anyio.to_thread.run_sync`

> Inside async services (`services/catalog.search_catalog`, `browse`,
> `by_bild_ids`, `by_bild_id`), the Lance `ds.scanner(...).to_table()`
> calls are wrapped with `anyio.to_thread.run_sync(...)`.

**Why.**

- Lance has no async API; the scanner blocks until the dataset returns.
  Calling a blocking function inside `async def` ties up the event loop
  for the duration of the I/O — that's the #1 anti-pattern in
  `fastapi/references/anti-patterns.md`.
- Sync service helpers (`services/search.search_lines`, `catalog_stats`)
  stay `def` and are called from sync route handlers — FastAPI runs them
  in its threadpool automatically. Same effect, no wrapper needed.
- The rule of thumb: if a sync I/O call lives in the same module as
  `await session.execute(...)`, wrap it. If the whole service is sync,
  the endpoint stays sync and FastAPI handles the threadpool.

---

## 3. Trade-offs explicitly accepted

| Decision | Trade-off |
|---|---|
| Python pinned to 3.13 | Workspace would build on 3.14 for viewer alone, but `runner`/`htr` need torch which lacks a 3.14 wheel today. 3.13 keeps the whole workspace sync-able. Revisit when torch ships 3.14. |
| `ray[default]==2.55.1` exact pin | Locks the `JobDetails` schema to one Ray version. Upgrading Ray = updating viewer's response schema. Worth it for type safety; without it we're back to hand-rolled mapping. |
| No Alembic migrations | The batches table is *derived* from a CSV + manifest fetches. `scripts/build_batches_db.py` rebuilds it; SQLModel just reads. Add Alembic the day we have non-derivable data. |
| Subprocess shell-out still in `submission.py` | Logic for `sync_from_s3` and `submit_chunks` lives in `components/scripts/`. Bringing it into viewer is the heavy refactor — `services/submission.py` is the wrapper until then. |
| Lance scanner calls are sync inside `def` handlers | Lance has no async API. Sync handlers run in FastAPI's threadpool, which is correct per `fastapi/references/core-conventions.md` § Async vs sync. |
| `BatchesDbDep` removed entirely | All services now go through the ORM. The old fallback for "services not yet migrated" is gone — the migration is complete. |

---

## 4. What's NOT covered yet

- **Authentication** — viewer has none. The deploy assumes localhost/trusted
  network. `fastapi/references/authn.md` has the patterns for when this
  changes.
- **Health checks beyond liveness** — `/api/health` exists; proper
  `/livez` + `/readyz` with per-component checks haven't been added (see
  `fastapi/references/health-checks.md`).
- **Observability** — no OTel instrumentation yet (`fastapi/references/observability.md`).
- **Rate limiting** — none. Internal tool, no abuse vector yet.
- **Alembic** — see § 3.

---

## 5. Pointers

- Run / dev → `components/services/viewer/README.md`
- HTTP routes overview → `/api/docs` (Swagger) or `/api/redoc` (live)
- System diagram → `docs/architecture/system-overview.md`
- Skill conventions → `.claude/skills/fastapi/`,
  `.claude/skills/writing-python/`, `.claude/skills/python-infrastructure/`

---

## 6. FAQ — questions that come up reading this

### "Is the DB layer really agnostic?"

Yes. `Settings.resolved_database_url` returns whatever `DATABASE_URL`
env var is set to; only if unset does it fall back to a sqlite URL.
`make_engine` branches on URL scheme so pool flags only apply to
server DBs. All SQL goes through the SQLAlchemy expression API — no
dialect-specific raw strings.

```bash
# dev (sqlite, no extra deps)
RASK_VIEWER_INPUT=… RASK_VIEWER_OUTPUT=… uv run uvicorn viewer.main:app

# prod (postgres)
uv pip install -e components/services/viewer[postgres]
DATABASE_URL=postgresql+asyncpg://user:pass@host/db \
  RASK_VIEWER_INPUT=… RASK_VIEWER_OUTPUT=… \
  uv run uvicorn viewer.main:app
```

No code change. Same SQL, different driver.

### "What's a repository actually for?"

It owns SQL. Concretely:

- **Only `repositories/*.py` imports `select` / `update` / `delete`.** If
  a service is reaching for SQLAlchemy primitives, the query belongs
  in a repo method.
- Services compose multiple repo calls + map ORM rows → response
  schemas + call external systems (Lance, Ray, S3). They don't know
  SQL syntax.
- Endpoints parse HTTP and call services. They don't know about the
  ORM at all.

What we get from it:

| Without repository | With repository |
|---|---|
| SQL scattered across services | One file per aggregate owns the queries |
| Service tests need a real DB | Service tests can use a fake repo |
| Renaming a column → grep many files | Renaming a column → one file |
| Mixing query optimization into business logic | Repo method is the one place to add an index hint or rewrite a query |

### "Where will Alembic go when we add it?"

Standard layout (per `fastapi/references/database.md` § Migrations):

```
components/services/viewer/
├── alembic.ini                 # next to pyproject.toml
├── alembic/
│   ├── env.py                  # imports SQLModel.metadata; render_as_batch=True
│   ├── script.py.mako          # has `import sqlmodel` for autogen
│   └── versions/
│       └── 0001_initial.py
├── pyproject.toml
└── src/viewer/...
```

Init:

```bash
cd components/services/viewer
uv run alembic init -t async alembic
# Edit alembic/env.py:
#   from sqlmodel import SQLModel
#   from viewer import models  # noqa: F401  -- registers tables
#   target_metadata = SQLModel.metadata
#   context.configure(..., render_as_batch=True, compare_type=True, compare_server_default=True)
```

`render_as_batch=True` is required for sqlite (no real ALTER TABLE).
Leave it on even when prod is postgres — local sqlite tests run the
same migrations.

The naming convention block in `core/db.py` is already in place so
constraint names are stable from the first migration.

### "Are there dialect gotchas between postgres and sqlite I need to think about?"

Yes — listed in § 4 below. Short version: viewer's current schema is
conservatively portable (TEXT timestamps, no JSON columns, no UUID
columns, every GROUP BY selects only grouped cols). The biggest
forward concern is **timestamps stored as TEXT**: if we ever want
server-side date arithmetic, we'd switch to `DateTime(timezone=True)`
and that's where dialects diverge (sqlite stores as TEXT, postgres as
TIMESTAMPTZ; comparison semantics differ).

### "Are we using SQLModel + Pydantic correctly? Any weird overlap?"

No overlap. Clear split:

- `models/batch.py` — SQLModel trinity. The only thing the ORM touches.
- `schemas/*.py` — Pydantic wrappers (response envelopes,
  composites). Imports `BatchPublic` from models for the row shape.
- Services convert ORM → schema via `BatchPublic.model_validate(orm)`.
  Works because SQLModel sets `from_attributes=True` on its base config.

Why the **trinity** (Base / Table / Public) and not one class? See
§ 2.7 — separates API contract from DB schema; scales when we add
write endpoints (`BatchCreate`, `BatchUpdate` slot into the same
hierarchy).

### "Why is `services/submission.py` still shelling out?"

The business logic for `sync_from_s3` and `submit_chunks` physically
lives in `components/scripts/*.py`. Bringing it into viewer means:

1. Extracting reconciliation + Ray-submission code into a new
   workspace package — proposed name `packages/control/`
2. Both viewer (`from control.sync import reconcile_from_s3`) and the
   standalone CLI scripts (`def main(): asyncio.run(reconcile_from_s3())`)
   import the same module
3. `services/submission.py` collapses to direct service calls

This has nothing to do with `storage` — `storage` is the S3 wrapper
(building boto3 clients, listing buckets). What we're shelling out to
is *business logic* (reconciling DB vs S3 counts, building Ray
entrypoints, updating `current_rayjob_id`).

Until that extraction lands, `services/submission.py` runs the script
via `asyncio.create_subprocess_exec` so it doesn't block the event
loop — but yes, it's a code smell, deliberately accepted.

---

## 7. Dialect gotchas — postgres vs sqlite cheatsheet

| Concern | sqlite | postgres | Viewer's stance |
|---|---|---|---|
| `ALTER TABLE` | No `DROP COLUMN` < 3.35, no `ALTER COLUMN` | Full support | Alembic `render_as_batch=True` recreates the table for sqlite — same migration script runs on both. |
| Timestamps | TEXT (no real type) | `TIMESTAMP` / `TIMESTAMPTZ` native | Stored as TEXT (`fetched_at`, `started_at`, …). Comparisons happen in Python after fetch. |
| JSON / JSONB | No JSON type (TEXT) | `JSONB`, indexable, queryable | Viewer stores no JSON columns. |
| UUID | TEXT-only | Native `UUID` | Viewer uses string PKs. |
| `GROUP BY` strictness | Lax (allows non-grouped SELECT columns) | Strict | Every viewer GROUP BY selects only grouped cols + aggregates. Portable. |
| `func.random()` | `RANDOM()` | `random()` | SQLAlchemy normalizes — works in both. |
| `func.now()` return type | TEXT | `TIMESTAMPTZ` | Viewer doesn't compare server-side. |
| Concurrency | Single writer, WAL | MVCC | SQLite fine for dev/single-replica; postgres for multi-replica. |
| Transaction isolation | Serializable default | Read committed default | Repo methods are short — no current isolation concerns. |
| Connection pool | Single in-process connection | Real pool | `make_engine` branches: sqlite no flags, postgres gets `pool_size`/`pool_pre_ping`/`pool_recycle`. |
| `LIMIT` / `OFFSET` | Same syntax | Same syntax | Portable. |
| `IN (…)` placeholders | `?` (SQLAlchemy abstracts) | `$1, $2` (SQLAlchemy abstracts) | `col(Batch.batch_id).in_(ids)` — portable. |
| `EXCEPT` / `INTERSECT` | Supported | Supported | Portable if we add set ops. |
| CTE / `WITH` | Supported | Supported | Portable. |

**Rules currently being respected in repository code:**

1. Don't write raw `f"SELECT ... {value}"` SQL. Always use SQLAlchemy
   expressions or `?`-parameterized queries.
2. No JSON, ARRAY, UUID column types. Anything structured goes
   through the application layer.
3. Every `func.X()` we use is in both dialects (currently: `count`,
   `coalesce`, `sum`, `max`, `random`, `case`).
4. No `WITH RECURSIVE`, no `RETURNING` (sqlite added `RETURNING` in
   3.35 but earlier versions ship in some distros; avoid until
   needed).
5. Connection pool flags are conditional on URL scheme — keeps sqlite
   single-connection (its safe model) and gives postgres a real pool.

When we cross any of these lines, write a portability note in the
repository method comment.
