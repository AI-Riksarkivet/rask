# Data Model

!!! warning "P7a (2026-07-27): the batches/orchestrator plane described below is DELETED"
    The compute-plane cutover (`lance-ns-merge.md` P7a) removed the orchestrator loop + entrypoint
    (`:8810`), the `batches` table + Alembic lineage, S3-sync, chunk submission, and the prefetch lane.
    Ingestion is now the medallion producer's `POST /ingest-iiif` (IIIF → raw page-image Lance dataset,
    ONE raw-write OpenLineage event) and HTR runs as event-driven cascade compute on the unified Ray
    cluster. Sections referring to batches/chunks/orchestrator are kept as historical context until the
    P8 doc re-draw.

rask keeps two distinct data stores. The **relational `batches` table** is the
control plane — what to process and how far it's got. The **Lance tables** on S3
are the search/read plane — the transcribed content you query. They are linked
logically by `batch_id`.

```mermaid
erDiagram
    batches ||--o{ lines : "batch_id  (1 batch → many lines)"
    batches ||--o| archive_catalog : "batch_id == bild_id"

    batches {
        string batch_id PK "archival batch id"
        string arkiv_referenskod "archive metadata (master CSV)"
        string arkiv_titel
        string volym
        string rattighetsmarkning_volym
        string rattighetsmarkning_batch
        string startdatum
        string slutdatum
        string htrad_tidigare
        int page_count "from IIIF manifest"
        string iiif_endpoint
        enum manifest_status "ok|http_403|http_400|error|pending"
        string manifest_error
        string fetched_at
        int cached_pages "default 0"
        int transcribed_pages "default 0"
        enum htr_status "pending|cached|partial|done|verification_failed"
        string started_at
        string finished_at
        string last_error
        string last_synced_at
        int chunk_id "groups batches into a submission unit"
        int chunk_total
        string current_rayjob_id "Ray job tracking"
        string current_rayjob_submitted_at
    }

    lines {
        string batch_id "join key"
        string page_id
        int page_idx
        string line_id
        int line_idx
        string text "full-text indexed"
        float confidence
        float hpos
        float vpos
        float width
        float height
        string polygon "ALTO POINTS -> [[x,y]]"
        string thumb_key
    }

    archive_catalog {
        string bild_id "== batch_id"
        string search_text "full-text indexed"
        string tier "listed|cached|transcribed"
        string EAD_fields "+ archival description cols"
    }
```

!!! note "Lance tables are not relational"
    `lines` and `archive_catalog` are **LanceDB** datasets in
    `s3://images-batch-search`, not SQL tables — their links to `batches` are
    logical (by `batch_id`), not enforced foreign keys.

## The relational DB — a single `batches` table

A backend-agnostic ORM (SQLModel + SQLAlchemy async): **SQLite in dev**
(`.cache/batches.db`), **Postgres in prod** (via `DATABASE_URL`), schema managed
by **Alembic**. There is exactly one table, `batches`, keyed by `batch_id`. There
are **no foreign keys and no separate `chunks` table** — a "chunk" is just a set
of rows sharing a `chunk_id`.

The columns fall into five groups:

| Group | Columns | Maintained by |
|---|---|---|
| Identity & archival metadata | `batch_id` (PK), `arkiv_referenskod`, `arkiv_titel`, `volym`, `rattighetsmarkning_volym/batch`, `startdatum`, `slutdatum`, `htrad_tidigare` | `build_batches_db.py` (master CSV) |
| IIIF / manifest | `page_count`, `iiif_endpoint`, `manifest_status`, `manifest_error`, `fetched_at` | manifest fetch |
| HTR progress | `cached_pages`, `transcribed_pages`, `htr_status`, `started_at`, `finished_at`, `last_error`, `last_synced_at` | `reconcile_from_s3` + pipeline |
| Chunking | `chunk_id`, `chunk_total` | chunk assignment |
| Ray job tracking | `current_rayjob_id`, `current_rayjob_submitted_at` | `submit_chunk` |

Two enums are stored as lowercase strings via `SAEnum(values_callable=…)`, so
they round-trip against both Postgres native ENUMs and SQLite VARCHAR:

- **`ManifestStatus`** — `ok`, `http_403`, `http_400`, `error`, `pending`
- **`HtrStatus`** — `pending`, `cached`, `partial`, `done`, `verification_failed`

!!! info "Constraint naming"
    `SQLModel.metadata.naming_convention` is pinned **before** the model class is
    defined, so Alembic autogenerates stable constraint names (anonymous names
    break rollbacks).

## The Lance tables — search/read plane

LanceDB datasets in `s3://images-batch-search`, queried by full-text search:

- **`lines`** — one row per transcribed text line (the line search index, ~2.76M
  rows). `text` is the FTS column; `polygon` is the ALTO `POINTS` string, coerced
  to `[[x, y], …]` by the API's `LineRow` validator.
- **`archive_catalog`** — EAD archival catalog descriptions; `search_text` is the
  FTS column, browsable by `tier`.

## How the planes connect

```mermaid
flowchart LR
    csv["master CSV"] -->|build_batches_db| b[("batches table<br/><sub>control plane</sub>")]
    b -->|chunk_id · cached ≥ 95%| orch["orchestrator submits Ray jobs"]
    s3in[("S3 · images-batch<br/>JPEGs")] -->|HTR| s3out[("S3 · images-batch-alto<br/>ALTO XML")]
    orch --> s3out
    s3out -->|index_alto| lance[("Lance · lines / archive_catalog<br/><sub>search plane</sub>")]
    b -.batch_id.-> lance
```

The `batches` table tracks *what to process and how far it's got*; the Lance
tables hold *the transcribed content you search*; S3 holds the actual images and
ALTO output. This separation is why an empty `batches.db` breaks the batch and
orchestrator endpoints but **not** search — search reads the Lance plane.
