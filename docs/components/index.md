# Components

`components/` holds the **runnable** code — the things that actually execute, as
opposed to the libraries in `packages/`.

| Path | Type | Docs |
|---|---|---|
| `components/apps/runner` | Python CLI (Ray Data jobs) | [Apps](apps.md) · [Projects → Runner](../projects/runner.md) |
| `components/apps/frontend` | SvelteKit SPA | [UI Components](ui.md) |
| `components/services/gateway` | Reverse proxy (`:8888`) | [Services](services.md) |
| `components/services/core` | Core domain brick (shared by core-api + orchestrator) | [Services](services.md) |
| `components/services/core_api` | Batches/chunks/catalog API (`:8801`) | [Services](services.md) |
| `components/services/orchestrator` | Orchestrator loop + endpoints (`:8810`) | [Services](services.md) |
| `components/services/volumes_api` | S3/IIIF image+ALTO proxy (`:8803`) | [Services](services.md) |
| `components/services/search_api` | Lance FTS + thumbnails (`:8802`) | [Services](services.md) |
| `components/services/ray_api` | Ray dashboard + Serve proxy (`:8804`) | [Services](services.md) |
| `components/scripts/` | One-shot Python tools | below |

## `components/scripts/`

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

- **[Apps](apps.md)** — the runner CLI and the frontend SPA.
- **[Services](services.md)** — the gateway, core brick, and the five per-domain services.
- **[UI Components](ui.md)** — the SvelteKit app and the `component-lib` library.
