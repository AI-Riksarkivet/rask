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

A **pipeline** is a named processing recipe. Each `PipelineSpec` declares its
lane (`Slot.HTR` or `Slot.PREFETCH`), its stages, and how it is launched:

| Pipeline | Kind | What it does |
|---|---|---|
| `htr` | runner | Actor-per-stage Ray Data pipeline (PageLoader → Layout → Lines → Transcribe → AltoExport). GPU. |
| `htrflow` | runner | Collapses layout/line/transcribe/ALTO into one Ray Serve deployment. |
| `htr_http` | http | A standalone driver that POSTs pages to the deployed `/htr` Serve endpoint — no heavy runner deps. |
| `prefetch` | runner | Warms the S3 image cache from IIIF. |
| `fake` | runner | No-GPU smoke pipeline that still writes ALTO. |

The two **shapes** are *actor-per-stage* (fine-grained Ray actors, GPU for
YOLO + TrOCR via Serve) and *single Serve deployment* (`htrflow` / `htr_http`).

## Runner & Ray Serve

The **runner** is the engine: each CLI invocation submits exactly one Ray Data
pipeline and blocks on `.materialize()`. It is not a long-lived service. **Ray
Serve** persists across job submissions — TrOCR weights stay resident in
`/transcribe` (or the full pipeline in `/htrflow` / `/htr`), so jobs call warm
models over in-process handles or HTTP.

## Orchestrator

The **orchestrator** is an in-process `asyncio` task inside the viewer that
replaces the old cron. Each tick it reconciles S3, then submits the next
eligible prefetch and HTR chunks to the Ray cluster. It is gated by
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
