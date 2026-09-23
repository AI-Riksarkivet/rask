# Concepts

The vocabulary you need to read the rest of the docs.

## Batch

A **batch** is the unit of archival work — typically one volume or series,
identified by a `batch_id`. Batches are tracked as rows in the batches DB with
status fields (`manifest_status`, `htr_status`) and page counts. The master list
is built once by `build_batches_db.py`; `sync` then reconciles each batch's
cached/transcribed page counts against S3.

## Chunk

A batch is split into **chunks** for submission so a single Ray job processes a
bounded set of pages. Chunk membership lives in the batches table
(`chunk_id`, `chunk_total`); a chunk's batches are submitted together as one
Ray job named `<pipeline>-chunk-NNN-of-MMM-<timestamp>`.

## Pipeline

A **pipeline** is a runner's own named processing recipe. The platform does not
enumerate them: which pipelines a workload offers, their stages, and which run
as Ray actors and which behind Ray Serve are that runner's business, documented
in its own README (`runners/<workload>/README.md`).

## Runner & Ray Serve

A **runner** is a sealed workload under `runners/<workload>` — see
**[Runners](../projects/runner.md)**. Each invocation submits exactly one Ray Data
pipeline and blocks on `.materialize()`; it is not a long-lived service. **Ray
Serve** persists across job submissions — a runner's model weights stay resident
in the Serve applications it owns, so jobs call warm models over in-process
handles or HTTP instead of reloading them.

## Orchestrator

The **orchestrator** is a lifespan-managed `asyncio` task running in the
`orchestrator` service (`:8810`). Each tick it reconciles S3, then submits the
next eligible prefetch and HTR chunks to the Ray cluster. It is gated by
`RASK_ORCHESTRATOR_AUTOSTART` and can be toggled at runtime via
`POST /api/v1/orchestrator/start` and `/stop`.

## State & storage

- **Batches DB** — relational state behind a backend-agnostic ORM (SQLModel +
  SQLAlchemy async). SQLite in dev, Postgres in prod (selected by
  `DATABASE_URL`); schema changes go through Alembic.
- **S3 two-bucket** — `images-batch` (input image cache) and
  `images-batch-alto` (ALTO output). Source images come from IIIF with an S3
  read-through cache.
- **Lance tables** — full-text `lines` and `archive_catalog` indexes on S3,
  queried by the search endpoints.

See **[Architecture](../architecture/index.md)** for how these pieces connect.
