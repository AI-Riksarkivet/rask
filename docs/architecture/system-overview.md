# rask — system overview (as-is)

Snapshot of the **current** architecture across runner, Ray, backend, frontend
and storage. No proposals here — see siblings (`frontend-monorepo.md`) for
direction.

## One-paragraph summary

`rask` is a distributed image-to-ALTO-XML pipeline for the Swedish National
Archives. A **Python CLI runner** submits **Ray Data** jobs that fan
**handwritten-text-recognition** (HTR) work across a **local Ray cluster**
with warm **TrOCR** weights kept resident in **Ray Serve**. Images come from
**IIIF** or pre-staged **S3** buckets; output ALTO XML lands back in **S3**.
A **FastAPI viewer service** exposes `/api/*` endpoints that a **SvelteKit
SPA** consumes for inspection and chunk-submission. Job-tracking state lives
in a tiny **SQLite** file (`.cache/batches.db`); there is **no Postgres**.

## Top-level component map

```mermaid
flowchart TB
    subgraph user["User"]
        browser["Browser"]
    end

    subgraph frontend["Frontend · SvelteKit SPA"]
        spa["components/apps/frontend<br/><sub>viewer · batch UI</sub>"]
    end

    subgraph backend["Backend · FastAPI"]
        viewer["components/services/viewer<br/><sub>/api/* on :8888</sub>"]
    end

    subgraph runner["Runner · Python CLI"]
        cli["components/apps/runner<br/><sub>Typer CLI, Ray Data jobs</sub>"]
        scripts["components/scripts/<br/><sub>build/sync/chunk/submit</sub>"]
    end

    subgraph ray["Local Ray cluster (Makefile-managed)"]
        head["Ray head :6379<br/>dashboard :8265"]
        workers["Worker actors<br/><sub>PageLoader · Layout · Lines</sub>"]
        serve["Ray Serve<br/><sub>/transcribe (TrOCR)<br/>/htrflow (full pipe)</sub>"]
    end

    subgraph storage["Storage"]
        s3in[("S3 · images-batch")]
        s3out[("S3 · images-batch-alto")]
        sqlite[(".cache/batches.db<br/><sub>SQLite</sub>")]
        iiif[("IIIF<br/><sub>Riksarkivet</sub>")]
    end

    subgraph htrlib["Library code"]
        htr["packages/htr<br/><sub>Ray actors, schemas</sub>"]
        storagepkg["packages/storage<br/><sub>FS/S3/IIIF abstractions</sub>"]
    end

    browser --> spa
    spa -->|"/api/*"| viewer
    viewer -->|read images| s3in
    viewer -->|read ALTO| s3out
    viewer -->|read/write rows| sqlite
    viewer -.->|"/api/ray/* proxy"| head
    viewer -.->|submit job| head

    cli --> head
    scripts --> head
    scripts --> sqlite

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
    class s3in,s3out,sqlite,iiif store
    class htr,storagepkg lib
```

## What lives where

| Path                                | Type          | Purpose                                                        |
| ----------------------------------- | ------------- | -------------------------------------------------------------- |
| `components/apps/frontend/`         | SvelteKit SPA | Browser UI: page viewer, batch dashboard, Ray-dashboard proxy  |
| `components/apps/runner/`           | Python CLI    | Submits Ray Data jobs; ships Ray Serve deployments             |
| `components/services/viewer/`       | FastAPI       | Only HTTP backend; `/api/*` on `:8888`; no auth                |
| `components/scripts/`               | Python        | One-shot tools: `build_batches_db`, `sync_from_s3`, `chunk_*`  |
| `packages/htr/`                     | Python lib    | Ray actors (PageLoader, Layout, Lines, Transcribe, AltoExport) |
| `packages/storage/`                 | Python lib    | `FSSource/Sink`, `S3Source/Sink`, `IIIFCachedSource`           |
| `packages/component-lib/`           | TS / Svelte   | (Future) shared UI library — see `frontend-monorepo.md`        |
| `.cache/batches.db`                 | SQLite        | Per-batch progress tracking (built locally, not committed)     |
| `Makefile`                          | bash          | All deploy/dev orchestration (no docker-compose, no k8s yaml)  |

## Data flow — image → ALTO XML

The runner is the engine. Submits one Ray Data pipeline per invocation, then
blocks until `.materialize()` returns.

```mermaid
flowchart LR
    src[("source<br/>--input s3:// or iiif://")]
    diff["runner CLI<br/><sub>diff source vs sink</sub>"]

    subgraph raydata["Ray Data pipeline (per batch)"]
        pl["PageLoaderActor<br/><sub>S3 hit · IIIF miss</sub>"]
        lay["LayoutActor<br/><sub>YOLO regions · 0.001 GPU</sub>"]
        ln["LineActor<br/><sub>YOLO lines · 0.001 GPU</sub>"]
        tr["TranscribeViaServe<br/><sub>CPU actor → Serve handle</sub>"]
        ex["AltoExportActor<br/><sub>→ ALTO XML string</sub>"]
        wr["AltoWriterActor<br/><sub>→ S3</sub>"]
    end

    serve["Ray Serve · /transcribe<br/><sub>TrOCR · 3 replicas × 0.99 GPU<br/>warm across job submissions</sub>"]
    sink[("sink<br/>s3://images-batch-alto/{batch}/{key}.xml")]

    src --> diff
    diff --> pl
    pl --> lay
    lay --> ln
    ln --> tr
    tr -.synchronous call.-> serve
    serve -.transcribed lines.-> tr
    tr --> ex
    ex --> wr
    wr --> sink

    style serve fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
    style src fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style sink fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
```

**Alternate path:** `htrflow` pipeline collapses Layout+Line+Transcribe+Alto
into a single Ray Serve deployment (`/htrflow`, 1 replica, CPU-only). Used
when GPU isn't worth the actor-fan-out overhead for a given batch shape.

## Batch lifecycle — CSV → SQLite → Ray job → S3

How a batch becomes a queued, then submitted, then transcribed unit.

```mermaid
sequenceDiagram
    autonumber
    participant CSV as master CSV
    participant Build as build_batches_db.py
    participant DB as .cache/batches.db (SQLite)
    participant Sync as sync_from_s3.py
    participant S3i as S3 · images-batch
    participant S3o as S3 · images-batch-alto
    participant UI as Frontend
    participant API as Viewer /api
    participant Sub as submit_chunks.py
    participant Ray as Ray head

    CSV->>Build: read 1633 batch IDs
    Build->>DB: INSERT batches (page_count, manifest_status, …)
    Sync->>S3i: count cached pages
    Sync->>S3o: count transcribed pages
    Sync->>DB: UPDATE cached_pages, transcribed_pages
    UI->>API: GET /api/batches
    API->>DB: SELECT *
    DB-->>UI: render dashboard
    UI->>API: POST /api/batches/sync
    API->>Sync: re-run
    UI->>API: GET /api/chunks
    UI->>API: POST /api/chunks/{id}/submit
    API->>Sub: submit
    Sub->>Ray: submit Ray Job (chunk)
    Ray->>DB: (runner) updates current_rayjob_id
    Ray->>S3o: writes ALTO XMLs
    UI->>API: poll /api/batches
    API->>DB: SELECT
    DB-->>UI: transcribed_pages ↑
```

## Frontend ↔ Backend ↔ Storage

What the SPA actually fetches.

```mermaid
flowchart LR
    spa["SvelteKit SPA"]
    api["FastAPI viewer :8888"]

    subgraph endpoints[" "]
        e1["/api/health"]
        e2["/api/volumes/{vol}/pages"]
        e3["/api/volumes/{vol}/pages/{key}/image"]
        e4["/api/volumes/{vol}/pages/{key}/alto"]
        e5["/api/batches · /api/batches/sync"]
        e6["/api/chunks · /api/chunks/{id}/submit"]
        e7["/api/ray/* (dashboard proxy)"]
    end

    s3[("S3<br/>images-batch · images-batch-alto")]
    db[(SQLite<br/>.cache/batches.db)]
    rayhead["Ray head :8265"]

    spa --> e1
    spa --> e2
    spa --> e3
    spa --> e4
    spa --> e5
    spa --> e6
    spa --> e7

    e2 --> s3
    e3 --> s3
    e4 --> s3
    e5 --> db
    e6 --> db
    e6 -.submits.-> rayhead
    e7 -.proxies.-> rayhead

    style spa fill:#312e81,stroke:#a78bfa,color:#e9e9ea
    style api fill:#0f766e,stroke:#5eead4,color:#e9e9ea
    style s3 fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style db fill:#1e3a8a,stroke:#60a5fa,color:#e9e9ea
    style rayhead fill:#3f3f46,stroke:#fbbf24,color:#e9e9ea
```

**Auth:** none. `viewer` has no middleware. Assumes localhost or trusted
network.

## Ray cluster topology

What `make ray-up` actually starts and what `make serve-up` deploys onto it.

```mermaid
flowchart TB
    subgraph head["Ray head node (local · :6379)"]
        gcs["GCS · scheduler · dashboard :8265"]
    end

    subgraph workers["Worker actors (per Ray Data job)"]
        pl["PageLoader<br/><sub>CPU · 1 per stream</sub>"]
        ly["Layout<br/><sub>0.001 GPU · YOLO regions</sub>"]
        ln["Lines<br/><sub>0.001 GPU · YOLO lines</sub>"]
        tx["TranscribeViaServe<br/><sub>CPU · blocks on Serve</sub>"]
        ax["AltoExport<br/><sub>CPU</sub>"]
        aw["AltoWriter<br/><sub>CPU · S3 PUT</sub>"]
    end

    subgraph serve["Ray Serve (persistent across jobs)"]
        trserve["/transcribe<br/><sub>TrOCR · 3 replicas × 0.99 GPU</sub>"]
        hfserve["/htrflow<br/><sub>1 replica · 4 CPU</sub>"]
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
`components/apps/runner/src/runner/pipeline.py` — assumes a **3-GPU node**:
3 Serve TrOCR replicas (≈ 0.99 GPU each) + ~6 layout/lines actors at
0.001 GPU. Fits one well-provisioned box.

**Remote KubeRay?** The CLI accepts `--address ray://dev-kuberay.ra.se:10001`,
suggesting an out-of-repo cluster exists. No Helm chart or K8s manifest lives
in this repo.

## Stack at a glance

| Concern             | Choice                                            |
| ------------------- | ------------------------------------------------- |
| Distributed compute | Ray Data + Ray Serve                              |
| Backend HTTP        | FastAPI (single service)                          |
| Frontend            | SvelteKit (SPA, adapter-static)                   |
| Object storage      | S3 (boto3) — two buckets: images-batch, *-alto    |
| Job-tracking state  | SQLite (`.cache/batches.db`, not committed)       |
| Source              | IIIF (Riksarkivet) with S3 read-through cache     |
| Models              | YOLO (regions, lines), TrOCR (transcription)      |
| Python              | uv + Ruff + ty (3.13)                             |
| JS / TS             | Bun + Vite + ESLint + Prettier (Svelte 5)         |
| Rust                | Cargo workspace (small support crates)            |
| Deploy artefacts    | None checked in — `Makefile` is the only runbook  |

## What's deliberately NOT here

- **No Postgres**, no MySQL, no Redis. State is SQLite + S3.
- **No docker-compose**, no Helm chart, no Kubernetes manifests in the repo.
- **No auth** on the viewer service. Localhost / trusted-network only.
- **No CI manifests** for cluster deployment. Remote Ray cluster is
  managed outside this repo.
- **No queue** between viewer and Ray. The viewer's `/api/chunks/{id}/submit`
  calls the Python script which submits a Ray Job synchronously.
- **No event bus.** Components communicate via S3 keys + SQLite rows + Ray's
  own job/actor RPCs.
