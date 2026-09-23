# Architecture Overview

rask is a data-type-agnostic multimodal AI platform: a governed Lance lakehouse plus batch
processing, with a viewer/search front end. This section describes the system as it runs today.

## One-paragraph summary

A sealed **runner** (`runners/<workload>`) submits one **Ray Data** job per invocation
and blocks on materialization; it fans its work across a Ray cluster with model weights
kept resident in the **Ray Serve** applications the runner owns. Where the source bytes
come from and what shape the output takes are the runner's business: the platform takes
bytes in, governs the table that comes out, and records the lineage. The HTTP backend is
a fleet of FastAPI services behind a **gateway** on port 8888: the **ray**
service (dashboard introspection + Serve proxy), the **controlplane**, and the
lance lakehouse/media planes (`/api/catalog`, `/api/lineage`, `/api/explorer/*`).
A set of **SvelteKit 2 + Svelte 5 SSR** apps (svelte-adapter-bun, served behind the
gateway) composed as routing-based microfrontend zones — a catch-all `home`
owning `/` plus per-domain apps (lakehouse/media/annotator/compute/studio/train) —
consumes all of these via the gateway. State lives in the governed Lance
lakehouse on S3; the batches DB died at P7a and the lines/EAD Lance tables
re-land catalog-governed behind `/api/explorer/search` (R6).

## Component map

```mermaid
flowchart TB
    browser["Browser"] --> spa["Frontend · SvelteKit SSR (Bun)<br/><sub>frontend/microfrontends/home</sub>"]
    spa -->|"/api/*"| gw["Gateway · :8888<br/><sub>services/gateway</sub>"]
    cli["Runner · Python CLI<br/><sub>runners/*</sub>"] --> head
    gw --> rayapi["ray · :8804"]
    gw --> cp["controlplane · :8820"]
    gw --> lake["lance lakehouse<br/>catalog · lineage · medallion"]
    gw --> media["lance media<br/>viewer · search · annotator"]
    media --> s3[("S3")]
    rayapi -.->|proxy| head["Ray head :6379 · dashboard :8265"]
    head --> serve["Ray Serve<br/><sub>runner-owned applications</sub>"]
    head --> workers["Worker actors<br/><sub>the runner's Ray Data stages</sub>"]
    workers -->|read source bytes| s3
    workers -->|write output| s3
```

## Key facts

- **The runner is the engine.** Each CLI invocation builds one `ray.data.Dataset`
  pipeline, triggers execution, prints `Done — ok=N, skipped=M`, and exits. It is
  not a long-lived service; the orchestrator service submits it as a Ray Job,
  one job per chunk.
- **Ray Serve persists across jobs.** A runner's model weights stay warm in the
  Serve applications it owns, and a pipeline stage calls them over a handle rather
  than loading the model per job. A deployment declares those applications in
  `ray.serveApplications[]`; replica counts and GPU fractions are the runner's
  settings, not the platform's.
- **A runner's stage graph is its own.** How many stages it has, which run as
  actors and which behind Serve, and how it packs GPUs live in
  `runners/<workload>` and that runner's README — see [Runners](../projects/runner.md).
- **No auth anywhere.** Only optional CORS plus request-id/timing headers; the
  fleet assumes a trusted/localhost network. The frontend hits `/api/*` on the
  gateway (SSR `load` uses the absolute gateway URL server-side);
  `/api/serve/*` is proxied by the compute service.
- **State** lives in the governed Lance lakehouse on S3 (the batches DB and the
  orchestrator died at P7a; core-api/search-api/volumes-api died in the R6/R20
  wave). A Helm chart in `chart/` deploys everything to Kubernetes, while the
  `Makefile` is the local/dev runbook.

## In this section

- **[Monorepo Layout](layout.md)** — the two language-pure workspace planes and what lives where.
- **[Data Flow](data-flow.md)** — image → ALTO XML, the batch lifecycle, and the frontend ↔ API ↔ storage map.
- **[Deployment](deployment.md)** — clusters, container images, CI, and how it ships.
- **[Microservices](microservices.md)** — the service decomposition history (June 2026) and its R6/R20 retirement down to gateway + ray + controlplane.

## Deep-dive notes (in-repo)

Longer design documents under `docs/architecture/` go beyond this summary:
[`system-overview.md`](system-overview.md),
[`viewer-backend.md`](viewer-backend.md) *(superseded — history/rationale for the dissolved viewer)*,
[`viewer-design.md`](viewer-design.md) *(superseded — history/rationale for the dissolved viewer)*,
[`frontend-microfrontends.md`](frontend-microfrontends.md),
[`frontend-monorepo.md`](frontend-monorepo.md).
