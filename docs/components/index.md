# Components

`components/` holds the **runnable** code — the things that actually execute, as
opposed to the libraries in `packages/`.

| Path | Type | Docs |
|---|---|---|
| `components/apps/runner` | Python CLI (Ray Data jobs) | [Apps](apps.md) · [Projects → Runner](../projects/runner.md) |
| `components/apps/frontend` | SvelteKit SPA | [UI Components](ui.md) |
| `components/services/viewer` | FastAPI backend (`:8888`) | [Services](services.md) · [Projects → Viewer](../projects/viewer.md) |
| `components/scripts/` | One-shot Python tools | below |

## `components/scripts/`

One-shot setup and debug tools — **no production-state-changing CLIs**. Anything
that mutates live state (sync, submit, orchestrate) goes through the viewer's
HTTP endpoints + the orchestrator loop instead. Notable scripts:

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
- **[Services](services.md)** — the viewer backend (endpoints, services, models).
- **[UI Components](ui.md)** — the SvelteKit app and the `component-lib` library.
