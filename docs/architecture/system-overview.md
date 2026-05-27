# rask — system overview (as-is)

Snapshot of the **current** architecture across runner, Ray, backend, frontend
and storage. No proposals here — see siblings (`frontend-monorepo.md`,
`viewer-backend.md`) for direction.

## One-paragraph summary

`rask` is a distributed image-to-ALTO-XML pipeline for the Swedish National
Archives. A **Python CLI runner** submits **Ray Data** jobs that fan
**handwritten-text-recognition** (HTR) work across a **local Ray cluster**
with model weights kept resident in **Ray Serve** (TrOCR on `/transcribe`,
full HTRflow pipeline on `/htrflow`). Images come from **IIIF** or
pre-staged **S3** buckets; output ALTO XML lands back in **S3**. A
**FastAPI viewer service** exposes versioned endpoints under `/api/v1/*`
that a **SvelteKit SPA** consumes for inspection, batch dashboard and
chunk submission. Batch-tracking state lives in a small relational DB
behind a backend-agnostic ORM — **SQLite for dev** (`.cache/batches.db`),
**Postgres for prod** — selected by `DATABASE_URL`. Full-text search over
transcribed lines + an archival catalog index live in optional **Lance**
tables on S3.

## Top-level component map

```mermaid
flowchart TB
    subgraph user["User"]
        browser["Browser"]
    end

    subgraph frontend["Frontend · SvelteKit SPA"]
        spa["components/apps/frontend<br/><sub>viewer · batch UI · search</sub>"]
    end

    subgraph backend["Backend · FastAPI"]
        viewer["components/services/viewer<br/><sub>/api/v1/* on :8888</sub>"]
    end

    subgraph runner["Runner · Python CLI"]
        cli["components/apps/runner<br/><sub>Typer CLI, Ray Data jobs</sub>"]
        scripts["components/scripts/<br/><sub>build/sync/chunk/submit/index</sub>"]
    end

    subgraph ray["Local Ray cluster (Makefile-managed)"]
        head["Ray head :6379<br/>dashboard :8265"]
        workers["Worker actors<br/><sub>PageLoader · Layout · Lines · TranscribeViaServe</sub>"]
        serve["Ray Serve<br/><sub>/transcribe (TrOCR)<br/>/htrflow (full pipe)</sub>"]
    end

    subgraph storage["Storage"]
        s3in[("S3 · images-batch")]
        s3out[("S3 · images-batch-alto")]
        relational[(".cache/batches.db (SQLite)<br/>or Postgres via DATABASE_URL")]
        lance[("Lance tables on S3<br/><sub>lines · archive_catalog<br/>optional</sub>")]
        iiif[("IIIF<br/><sub>Riksarkivet</sub>")]
    end

    subgraph libs["Library code"]
        htr["packages/htr<br/><sub>Ray actors, schemas</sub>"]
        storagepkg["packages/storage<br/><sub>FS/S3/IIIF abstractions</sub>"]
        control["packages/control<br/><sub>sync · chunk submission</sub>"]
        complib["packages/component-lib<br/><sub>Svelte 5 + Tailwind + Storybook</sub>"]
    end

    browser --> spa
    spa -->|"/api/v1/*"| viewer
    viewer -->|read images| s3in
    viewer -->|read ALTO| s3out
    viewer -->|read/write rows| relational
    viewer -->|search · catalog| lance
    viewer -.->|"/api/v1/ray/* proxy"| head
    viewer -.->|submit job| head

    cli --> head
    scripts --> head
    scripts --> relational
    scripts --> lance

    head --> workers
    head --> serve
    workers -->|read| s3in
    workers -->|read on miss| iiif
    workers -->|TrOCR call| serve
    workers -->|write ALTO| s3out

    cli -.imports.-> htr
    cli -.imports.-> storagepkg
    workers -.imports.-> htr
    viewer -.imports.-> storagepkg
    viewer -.imports.-> control
    scripts -.imports.-> control
    spa -.imports.-> complib

    classDef user fill:#1e293b,stroke:#94a3b8,color:#e9e9ea
    classDef fe fill:#312e81,stroke:#a78bfa,color:#e9e9ea
    classDef be fill:#0f766e,stroke:#5eead4,color:#e9e9ea
    classDef run fill:#7c2d12,stroke:#fdba74,color:#e9e9ea
    classDef rayc fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
    classDef store fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    classDef lib fill:#581c87,stroke:#c084fc,color:#e9e9ea

    class browser user
    class spa fe
    class viewer be
    class cli,scripts run
    class head,workers,serve rayc
    class s3in,s3out,relational,lance,iiif store
    class htr,storagepkg,control,complib lib
```

## What lives where

| Path                                | Type          | Purpose                                                              |
| ----------------------------------- | ------------- | -------------------------------------------------------------------- |
| `components/apps/frontend/`         | SvelteKit SPA | Browser UI: page viewer, batch dashboard, search, Ray-dashboard proxy |
| `components/apps/runner/`           | Python CLI    | Submits Ray Data jobs; ships Ray Serve deployments                   |
| `components/services/viewer/`       | FastAPI       | Only HTTP backend; `/api/v1/*` on `:8888`; no auth                   |
| `components/scripts/`               | Python        | One-shot tools: `build_batches_db`, `sync_from_s3`, `harvest_ead`, `search_index`, `submit_chunks` |
| `packages/htr/`                     | Python lib    | Ray actors (PageLoader, Layout, Lines, Transcribe, AltoExport)       |
| `packages/storage/`                 | Python lib    | `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`                 |
| `packages/control/`                 | Python lib    | Shared ops logic — sync, chunk submission — used by viewer + scripts |
| `packages/component-lib/`           | TS / Svelte   | Shared Svelte 5 + Bits UI + Tailwind 4 component library w/ Storybook |
| `.cache/batches.db`                 | SQLite        | Default per-batch progress (dev; not committed)                      |
| `.docker/*.dockerfile`              | Docker        | Image definitions for `viewer`, `runner` (CUDA), `frontend` (nginx)  |
| `Makefile`                          | bash          | All deploy/dev orchestration (no docker-compose, no k8s yaml)        |

## Data flow — image → ALTO XML

The runner is the engine. Submits one Ray Data pipeline per invocation, then
blocks until `.materialize()` returns.

```mermaid
flowchart LR
    src[("source<br/>--input s3:// or iiif://")]
    diff["runner CLI<br/><sub>diff source vs sink</sub>"]

    subgraph raydata["Ray Data pipeline (per batch)"]
        pl["PageLoaderActor<br/><sub>6 CPU workers · S3 hit · IIIF miss</sub>"]
        lay["LayoutActor<br/><sub>2 workers · YOLO regions · 0.001 GPU</sub>"]
        ln["LineActor<br/><sub>2 workers · YOLO lines · 0.001 GPU</sub>"]
        tr["TranscribeViaServe<br/><sub>8 CPU workers → Serve handle</sub>"]
        ex["AltoExportActor<br/><sub>→ ALTO XML string</sub>"]
        wr["AltoWriterActor<br/><sub>→ S3</sub>"]
    end

    serve["Ray Serve · /transcribe<br/><sub>TrOCR · 3 replicas × 0.99 GPU<br/>max_ongoing_requests=2<br/>warm across job submissions</sub>"]
    sink[("sink<br/>s3://images-batch-alto/{batch}/{key}.xml")]

    src --> diff
    diff --> pl
    pl --> lay
    lay --> ln
    ln --> tr
    tr -.synchronous handle call.-> serve
    serve -.transcribed lines.-> tr
    tr --> ex
    ex --> wr
    wr --> sink

    style serve fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
    style src fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style sink fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
```

**Pool sizing** uses `compute=ActorPoolStrategy(size=N)` (autoscaler off) —
empirically `concurrency=(N, N)` left the autoscaler in play. The GPU-heavy
transcription step is decoupled: `TranscribeViaServe` runs as 8 CPU map
workers, each blocking on a Ray Serve handle to the 3 GPU-resident TrOCR
replicas.

**Alternate path:** the `htrflow` pipeline collapses Layout+Line+Transcribe+
Alto into a single Ray Serve deployment (`/htrflow`, 3 replicas × 1 GPU + 2
CPU, `max_ongoing_requests=4`). Used when actor fan-out isn't worth it for a
given batch shape.

## Batch lifecycle — CSV → DB → Ray job → S3

How a batch becomes a queued, then submitted, then transcribed unit. The
"DB" here is `.cache/batches.db` (SQLite) in dev or Postgres in prod — the
ORM (SQLModel + SQLAlchemy async) is the same either way.

```mermaid
sequenceDiagram
    autonumber
    participant CSV as master CSV
    participant Build as build_batches_db.py
    participant DB as batches DB<br/>(SQLite or Postgres)
    participant Sync as sync_from_s3.py
    participant S3i as S3 · images-batch
    participant S3o as S3 · images-batch-alto
    participant UI as Frontend
    participant API as Viewer /api/v1
    participant Sub as submit_chunks.py
    participant Ray as Ray head

    CSV->>Build: read batch IDs
    Build->>DB: INSERT batches (page_count, manifest_status, …)
    Sync->>S3i: count cached pages
    Sync->>S3o: count transcribed pages
    Sync->>DB: UPDATE cached_pages, transcribed_pages
    UI->>API: GET /batches
    API->>DB: SELECT
    DB-->>UI: render dashboard
    UI->>API: POST /batches/sync
    API->>Sync: re-run (via packages/control)
    UI->>API: GET /chunks
    UI->>API: POST /chunks/{id}/submit
    API->>Sub: submit
    Sub->>Ray: submit Ray Job (chunk)
    Ray->>DB: (runner) updates current_rayjob_id
    Ray->>S3o: writes ALTO XMLs
    UI->>API: GET /orchestrator/state
    API->>DB: SELECT
    API->>Ray: dashboard probe (V1+V2 JobDetails)
    API-->>UI: unified job + batch state
```

## Frontend ↔ Backend ↔ Storage

What the SPA actually fetches. All API routes are under the
`RASK_API_PREFIX` (default `/api/v1`).

```mermaid
flowchart LR
    spa["SvelteKit SPA"]
    api["FastAPI viewer :8888<br/>/api/v1"]

    subgraph endpoints[" "]
        e1["/health"]
        e2["/volumes/{vol}/pages<br/>/pages/{key}/image<br/>/pages/{key}/alto"]
        e3["/batches · /batches/{id}<br/>/batches/{id}/catalog<br/>/batches/sync · /batches/random"]
        e4["/chunks · /chunks/{id}/submit"]
        e5["/search · /search/stats<br/>/search/thumb/{path}"]
        e6["/catalog/search · /catalog/browse<br/>/catalog/search/stats"]
        e7["/orchestrator/state"]
        e8["/ray/health · /ray/jobs · /ray/cluster<br/>+ dashboard proxy"]
    end

    s3[("S3<br/>images-batch · images-batch-alto")]
    db[("Batches DB<br/>SQLite or Postgres")]
    lance[("Lance tables<br/>lines · archive_catalog")]
    rayhead["Ray head :8265"]

    spa --> e1
    spa --> e2
    spa --> e3
    spa --> e4
    spa --> e5
    spa --> e6
    spa --> e7
    spa --> e8

    e2 --> s3
    e3 --> db
    e4 --> db
    e4 -.submits.-> rayhead
    e5 --> lance
    e5 --> s3
    e6 --> lance
    e7 --> db
    e7 -.probes.-> rayhead
    e8 -.proxies.-> rayhead

    style spa fill:#312e81,stroke:#a78bfa,color:#e9e9ea
    style api fill:#0f766e,stroke:#5eead4,color:#e9e9ea
    style s3 fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style db fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style lance fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style rayhead fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
```

**Middleware:** `RequestIDMiddleware`, `TimingMiddleware`, CORS. **Auth:**
none — `viewer` assumes localhost or trusted network.

**Lance is optional.** If HCP S3 credentials are absent the search and
catalog endpoints surface gracefully; nothing in the core image → ALTO
pipeline depends on Lance. Indexing is driven by scripts
(`make search-index`, `make catalog-index`, `make harvest-ead`).

**Orchestrator endpoint** (`/orchestrator/state`) is a pure-derivation view
that joins Ray job state (bridging Ray's V1 `JobInfo` and V2 `JobDetails`)
with batches-DB rows, so the SPA can poll one URL instead of fanning out.

## Ray cluster topology

What `make ray-up` actually starts and what `make serve-up` deploys onto it.

```mermaid
flowchart TB
    subgraph head["Ray head node (local · :6379)"]
        gcs["GCS · scheduler · dashboard :8265"]
    end

    subgraph workers["Worker actors (per Ray Data job)"]
        pl["PageLoader<br/><sub>6 CPU workers</sub>"]
        ly["Layout<br/><sub>2 workers · 0.001 GPU · YOLO regions</sub>"]
        ln["Lines<br/><sub>2 workers · 0.001 GPU · YOLO lines</sub>"]
        tx["TranscribeViaServe<br/><sub>8 CPU workers · blocks on Serve handle</sub>"]
        ax["AltoExport<br/><sub>CPU</sub>"]
        aw["AltoWriter<br/><sub>CPU · S3 PUT</sub>"]
    end

    subgraph serve["Ray Serve (persistent across jobs)"]
        trserve["/transcribe<br/><sub>TrOCR · 3 replicas × 0.99 GPU + 1 CPU<br/>max_ongoing_requests=2</sub>"]
        hfserve["/htrflow<br/><sub>3 replicas × 1 GPU + 2 CPU<br/>max_ongoing_requests=4</sub>"]
    end

    gcs --> workers
    gcs --> serve
    tx -.handle.-> trserve

    classDef head fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
    classDef worker fill:#7c2d12,stroke:#fdba74,color:#e9e9ea
    classDef serve fill:#581c87,stroke:#c084fc,color:#e9e9ea
    class gcs head
    class pl,ly,ln,tx,ax,aw worker
    class trserve,hfserve serve
```

**GPU sizing** is hardcoded in
`components/apps/runner/src/runner/pipeline.py` and the two Serve modules
(`runner/transcribe_service.py`, `runner/htrflow_service.py`). The numbers
target a **3-GPU node**: 3 TrOCR replicas at 0.99 GPU each fill the GPUs,
while Layout/Lines actors hold 0.001 GPU slots just to land them on the
GPU node. Earlier attempts at 6 transcribe replicas OOM'd host RAM
(6 × ~4 GB TrOCR weights).

**Remote KubeRay?** The CLI accepts `--address ray://dev-kuberay.ra.se:10001`,
suggesting an out-of-repo cluster exists. No Helm chart or K8s manifest
lives in this repo.

## Container images

Production-shaped image definitions live at `.docker/`. They are
**build-ready, not orchestrated** — there is no docker-compose, no Helm
chart, no Kustomize.

| Image      | Dockerfile                     | Base                           | Notes                                |
| ---------- | ------------------------------ | ------------------------------ | ------------------------------------ |
| `viewer`   | `.docker/viewer.dockerfile`    | `python:3.13-slim`             | uv install, runs `viewer.main:app`   |
| `runner`   | `.docker/runner.dockerfile`    | `nvidia/cuda:12.4-runtime`     | uv install, GPU client for Ray jobs  |
| `frontend` | `.docker/frontend.dockerfile`  | Bun build → `nginx-unprivileged` | Static SPA + `frontend.nginx.conf`  |

`.dockerignore` and `.hadolint.yaml` sit alongside them; the build context
is the repo root.

## Stack at a glance

| Concern             | Choice                                                                |
| ------------------- | --------------------------------------------------------------------- |
| Distributed compute | Ray Data + Ray Serve                                                  |
| Backend HTTP        | FastAPI (single service, `/api/v1/*`)                                 |
| ORM                 | SQLModel + SQLAlchemy async (aiosqlite or asyncpg)                    |
| Relational DB       | SQLite (dev, `.cache/batches.db`) or Postgres (prod, `DATABASE_URL`)  |
| Search / catalog    | Lance tables on S3 (optional, HCP-backed)                             |
| Frontend            | SvelteKit (SPA, adapter-static) + `packages/component-lib`            |
| Object storage      | S3 via `packages/storage` — two buckets: `images-batch`, `*-alto`     |
| Source              | IIIF (Riksarkivet) with S3 read-through cache                         |
| Models              | YOLO (regions, lines), TrOCR (transcription)                          |
| Python              | uv + Ruff + ty (3.13)                                                 |
| JS / TS             | Bun + Vite + ESLint + Prettier (Svelte 5)                             |
| Rust                | Cargo workspace (small support crates)                                |
| Container images    | `.docker/*.dockerfile` (no orchestration manifests in repo)           |
| Deploy orchestration| None checked in — `Makefile` is the only runbook                      |

## What's deliberately NOT here

- **No queue** between viewer and Ray. `POST /api/v1/chunks/{id}/submit`
  shells out to `submit_chunks.py` which submits a Ray Job synchronously.
- **No event bus.** Components communicate via S3 keys, DB rows, and
  Ray's own job/actor RPCs.
- **No auth** on the viewer service. Localhost / trusted-network only.
- **No docker-compose, no Helm chart, no Kubernetes manifests.** Just
  the image definitions in `.docker/`.
- **No CI manifests** for cluster deployment. Remote Ray cluster is
  managed outside this repo.
- **No Redis, no MySQL.** The relational tier is SQLite or Postgres only.
