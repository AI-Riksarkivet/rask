# Viewer

!!! warning "Superseded (June 2026)"
    The `viewer` deployable described here no longer exists. The viewer monolith
    was dissolved into a gateway + per-domain services over a shared `core` brick;
    the API endpoints now live in `core-api` (batches/chunks/catalog) and
    `orchestrator`, fronted by the gateway on :8888. See
    `docs/architecture/microservices.md`. The text below is retained for
    historical reference.

`projects/viewer` composes the FastAPI viewer service (`viewer` + `storage`) into
a deployable. The viewer is the only HTTP backend — `/api/v1/*` on port **8888** —
and hosts the in-process orchestrator that drives the pipeline.

→ Symbol docs: **[API reference](../reference/viewer.md)**. Endpoint and service
breakdown: [Components → Services](../components/services.md).

## Running it

```bash
make viewer            # uvicorn viewer.main:app on :8888 --reload
```

The app factory (`create_app`) loads `.env`, derives HCP creds, validates that
the configured pipelines are registered, and mounts the v1 router under
`settings.api_prefix`. On startup the lifespan opens the Lance search tables,
builds the Ray client and (optionally) the S3 client, creates the DB engine, and
— if `RASK_ORCHESTRATOR_AUTOSTART` — starts the orchestrator task. Everything
optional degrades gracefully: missing S3 / Lance / Ray yield `ok=false` payloads
rather than failing startup, so the service boots (and tests run) fully offline.

## Configuration highlights

Full table: [Getting Started → Configuration](../getting-started/configuration.md).
The viewer's `Settings` reads `.env` once at startup. Notable fields:

- `RASK_VIEWER_INPUT` / `RASK_VIEWER_OUTPUT` — required `s3://` prefixes.
- `RASK_API_PREFIX` — moves all v1 routes **and** docs/openapi; server-built URLs
  (e.g. search `thumb_url`) derive from it, so never hardcode `/api/...`.
- `DATABASE_URL` — Postgres in prod; unset falls back to SQLite
  (`.cache/batches.db`).
- `RAY_DASHBOARD_URL` — the cluster the viewer submits to and proxies.
- `RASK_ORCHESTRATOR_*`, `RASK_HTR_PIPELINE`, `RASK_HTR_MAX_INFLIGHT` — the
  orchestrator's behaviour.

## State & migrations

ORM is **SQLModel + SQLAlchemy async**; the single `batches` table stores its
enums as lowercase strings (`SAEnum(values_callable=…)`) so they round-trip
against both Postgres native ENUMs and SQLite VARCHAR. Schema changes go through
**Alembic** — never `create_all` at startup.

```bash
make pg-up        # local Postgres:16 at :5432
make pg-migrate   # alembic upgrade head
```

CI proves a clean from-zero migration via Dagger (`dagger call migrate-up`).

## The orchestrator

A lifespan-managed `asyncio.Task` that ticks every
`RASK_ORCHESTRATOR_INTERVAL_SECONDS`: reconcile S3 (throttled), `derive_state`,
then submit each eligible prefetch and HTR chunk (HTR capped by
`RASK_HTR_MAX_INFLIGHT`). Toggle at runtime via `POST /api/v1/orchestrator/start`
and `/stop`; inspect with `GET /api/v1/orchestrator/state`.

!!! info "Transitional design"
    The in-process loop replaces an older cron and is explicitly a stopgap — the
    intended successor is a NATS JetStream consumer so the orchestrator can
    survive viewer restarts and scale horizontally.
