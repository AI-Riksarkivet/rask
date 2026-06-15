# Data Flow

How an image becomes searchable ALTO XML, and how the SPA reads it back.

## Image → ALTO XML (the `htr` pipeline)

The runner submits one Ray Data pipeline per chunk. The GPU-heavy transcription
step is decoupled onto Ray Serve so model weights stay warm across jobs.

```mermaid
flowchart LR
    src[("source<br/>IIIF · S3")] --> pl["PageLoaderActor<br/><sub>size 6 · S3 hit · IIIF miss</sub>"]
    pl --> lay["LayoutActor<br/><sub>YOLO regions · 0.001 GPU</sub>"]
    lay --> ln["LineActor<br/><sub>YOLO lines · 0.001 GPU</sub>"]
    ln --> tr["TranscribeViaServe<br/><sub>8 CPU workers → Serve handle</sub>"]
    tr -.handle call.-> serve["Ray Serve · /transcribe<br/><sub>TrOCR · warm GPU replicas</sub>"]
    serve -.lines.-> tr
    tr --> ex["AltoExportActor<br/><sub>→ ALTO 4.4 XML</sub>"]
    ex --> wr["AltoWriterActor"]
    wr --> sink[("S3 · images-batch-alto<br/>{batch}/{key}.xml")]
```

**Pool sizing** uses `ActorPoolStrategy(size=N)` with the autoscaler off —
empirically `concurrency=(N, N)` biased work to whichever actor warmed first and
left GPUs idle. Each `TranscribeViaServe` task **shards its line crops three
ways** and fires three Serve calls at once, so all GPU replicas run concurrently
even though Ray Data keeps only one task in flight.

**Alternate shapes:** `htrflow` and `htr_http` collapse layout/line/transcribe/
ALTO into a single Serve deployment (`/htrflow`, `/htr`), used when actor
fan-out isn't worth a given batch shape — `htr_http` in particular is a
boto3-only HTTP driver that runs on clusters without the runner's heavy deps.

!!! info "Per-row resilience"
    `PageLoaderActor`, `LayoutActor`, and `LineActor` catch per-page exceptions
    and **drop the row** rather than failing the whole chunk; skipped pages
    surface as the gap between manifest counts and transcribed pages.

## Batch lifecycle

A batch becomes a queued, then submitted, then transcribed unit. The DB is
SQLite in dev or Postgres in prod — the ORM is the same either way.

```mermaid
sequenceDiagram
    autonumber
    participant Build as build_batches_db.py
    participant DB as batches DB
    participant Sync as reconcile_from_s3
    participant S3 as S3 (cache + alto)
    participant UI as Frontend
    participant API as Viewer /api/v1
    participant Orch as Orchestrator loop
    participant Ray as Ray head

    Build->>DB: INSERT batches (page_count, manifest_status)
    Sync->>S3: count cached / transcribed pages
    Sync->>DB: UPDATE counts + htr_status
    UI->>API: GET /batches
    API->>DB: SELECT
    Orch->>DB: derive eligible chunks (cached ≥ 95%)
    Orch->>Ray: submit Ray Job per chunk
    Ray->>S3: write ALTO XMLs
    UI->>API: GET /orchestrator/state
    API->>Ray: probe jobs + tasks
    API-->>UI: unified job + batch state
```

The orchestrator's `derive_state` excludes chunks already in flight or in a
failure cooldown, marks a chunk HTR-ready once **≥ 95%** of its pages are cached,
and only then submits — so ticks are idempotent.

## Frontend ↔ Backend ↔ Storage

All API routes sit under `RASK_API_PREFIX` (default `/api/v1`). The Ray
dashboard proxy is the exception — it lives at the root under `/api/serve/*`.

```mermaid
flowchart LR
    spa["SvelteKit SPA"] -->|/api/v1| api["FastAPI viewer :8888"]
    api -->|images · ALTO| s3[("S3")]
    api -->|batches · chunks · sync| db[("Batches DB")]
    api -->|line search · catalog| lance[("Lance tables")]
    api -.->|jobs · cluster · /api/serve proxy| ray["Ray dashboard :8265"]
```
