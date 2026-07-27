# Components

The **runnable** code — the things that actually execute, as opposed to the
libraries in `packages/`. Python deployables live in `services/`, the SvelteKit
zones in `frontend/microfrontends/`, and one-shot tools in `scripts/`.

| Path | Type | Docs |
|---|---|---|
| `runners/htr` | Python CLI (Ray Data jobs) | [Frontends](frontends.md) · [Projects → Runner](../projects/runner.md) |
| `frontend/microfrontends/home` | SvelteKit SSR app — catch-all, owns `/` (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/overview` | SvelteKit SSR app — `overview` domain (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/compute` | SvelteKit SSR app — `compute` domain (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/discover` | SvelteKit SSR app — `discover` domain (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/storage` | SvelteKit SSR app — `storage` domain (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/train` | SvelteKit SSR app — `train` domain (Bun server) | [UI Components](ui.md) |
| `frontend/microfrontends/studio` | SvelteKit SSR app — `studio` domain (Bun server) | [UI Components](ui.md) |
| `services/gateway` | Reverse proxy (`:8888`) | [Services](services.md) |
| `services/core` | Core domain package (shared by core-api + orchestrator) | [Services](services.md) |
| `services/core_api` | Batches/chunks/catalog API (`:8801`) | [Services](services.md) |
| `services/orchestrator` | Orchestrator loop + endpoints (`:8810`) | [Services](services.md) |
| `services/volumes_api` | S3/IIIF image+ALTO proxy (`:8803`) | [Services](services.md) |
| `services/search_api` | Lance FTS + thumbnails (`:8802`) | [Services](services.md) |
| `services/ray_api` | Ray dashboard + Serve proxy (`:8804`) | [Services](services.md) |
| `scripts/` | One-shot Python tools (+ the dev/ops shell scripts) | below |

## `scripts/`

One-shot setup and debug tools — **no production-state-changing CLIs**. Anything
that mutates live state (sync, submit, orchestrate) goes through the HTTP
services (core-api endpoints + the orchestrator service's lifespan loop). Notable scripts:

| Script | Purpose |
|---|---|
| `build_batches_db.py` | Build the master batches table from the source list. |
| `deploy_serve.py` | Deploy/undeploy the `transcribe` and `htrflow` Serve apps (`make serve-up`). |
| `harvest_ead.py` | Harvest EAD archival descriptions. |
| `index_alto.py` | Build the `lines` full-text Lance index from ALTO output (`make search-index`). |
| `index_catalog.py` | Build the `archive_catalog` Lance index. |
| `submit_index.py` | Submit the indexer as a tidy Ray Job. |
| `htr_chunk_job.py` | The `http`-kind driver that POSTs pages to a deployed `/htr` endpoint. |

## In this section

- **[Frontends](frontends.md)** — the runner CLI and the SSR frontend apps (catch-all + the six domain microfrontends: overview, compute, discover, storage, train, studio).
- **[Services](services.md)** — the gateway, core package, and the five per-domain services.
- **[UI Components](ui.md)** — the SvelteKit app and the `@rask/ui` library (frontend/packages/ui).
