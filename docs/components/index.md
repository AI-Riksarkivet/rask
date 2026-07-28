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
| `services/ray_api` | The `ray` service — Ray dashboard + Serve proxy (`:8804`) | [Services](services.md) |
| `services/controlplane` | Project provisioning (`:8820`) | [Services](services.md) |
| `services/{viewer,search,annotator}` | The lance media plane (`/api/media/*`; the viewer carries the S3 object browser) | [Services](services.md) |
| `services/{catalog,lineage,medallion,compaction}` | The lance lakehouse plane | [Services](services.md) |
| `scripts/` | One-shot Python tools (+ the dev/ops shell scripts) | below |

## `scripts/`

One-shot setup and debug tools — **no production-state-changing CLIs**. Anything
that mutates live state goes through the HTTP services. Notable scripts:

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
- **[Services](services.md)** — the gateway, the ray service, controlplane, and the lance lakehouse/media planes.
- **[UI Components](ui.md)** — the SvelteKit app and the `@rask/ui` library (frontend/packages/ui).
