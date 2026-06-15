# Local microservices trial — design

Date: 2026-06-15

## Goal

Run the rask backend as a set of **local microservice processes** behind a
gateway, so the proposed split from [microservices.md](../../architecture/microservices.md)
can be exercised end-to-end on a developer machine — no Docker images, no
Kubernetes. `make dev-micro` brings the whole fleet up; `make viewer-frontend`
talks to it unchanged.

## Approach: thin compositions, not a rewrite

Each service is a **composition over the existing, tested viewer code**, not a
code migration:

- Reuse `viewer.core.lifespan.make_lifespan` wholesale — it already builds every
  resource tolerantly (missing HCP/Lance/DB → `None`), so a service that only
  needs S3+Lance still boots cleanly with the same lifespan.
- Each service includes only the viewer endpoint routers it owns.
- The `viewer` package stays intact as a shared library **and** as a working
  monolith fallback (`make viewer` still runs the original).

This keeps the trial low-risk and fully reversible.

## Single trial brick

All service entrypoints live in **one new brick** `components/services/backends`
(python package `backends`), one module per service. This gives real separate
*processes* (independent uvicorn apps) without standing up six Polylith bricks.
A production split would later promote each module to its own brick + image; the
trial does not.

```
components/services/backends/
  pyproject.toml                 # deps: viewer, storage, fastapi, httpx, uvicorn
  src/backends/
    __init__.py
    _common.py                   # make_service_app(): settings + make_lifespan + routers
    core_api/app.py              # health, batches, chunks, catalog        :8801
    search_api/app.py            # health, search                          :8802
    volumes_api/app.py           # health, volumes                         :8803
    ray_api/app.py               # health, ray (+ /api/serve proxy)        :8804
    orchestrator/app.py          # health only; runs the loop via autostart :8810
    gateway/app.py               # httpx streaming reverse proxy           :8888
projects/backends/pyproject.toml # deployable composition
```

## Service responsibilities

| Service | Port | Routers (under `/api/v1`) | Notes |
|---|---|---|---|
| gateway | 8888 | — | path-routes `/api/*`; the frontend proxy target (unchanged) |
| core-api | 8801 | health, batches, chunks, catalog | owns Postgres `batches`; Ray submit for chunk ops |
| search-api | 8802 | health, search | LanceDB `lines` + S3; no DB |
| volumes-api | 8803 | health, volumes | S3/IIIF; no DB |
| ray-api | 8804 | health, ray + `proxy_router` | Ray dashboard; hosts `/api/serve/*` |
| orchestrator | 8810 | health | runs `run_loop` via `RASK_ORCHESTRATOR_AUTOSTART=1` |

The orchestrator stays the **in-process timer loop** for the trial (its own
process now, not inside an API). NATS JetStream is the eventual upgrade and is
out of scope here.

## Gateway

Standalone FastAPI (does not import `viewer`). Own `httpx.AsyncClient` in
lifespan; longest-prefix-first route table from env with localhost defaults:

```
/api/v1/search   → RASK_SEARCH_API_URL   (default http://127.0.0.1:8802)
/api/v1/volumes  → RASK_VOLUMES_API_URL  (default http://127.0.0.1:8803)
/api/v1/ray      → RASK_RAY_API_URL      (default http://127.0.0.1:8804)
/api/serve       → RASK_RAY_API_URL
/api/v1          → RASK_CORE_API_URL      (default http://127.0.0.1:8801)  # catch-all
```

Streaming proxy: forwards method/path/query/body/headers (minus `Host`), streams
the upstream response back. Matching is `path == prefix or path.startswith(prefix + "/")`.

## Running it locally

Dependencies via existing tooling:
- `make ray-up` (Ray; optional — services tolerate it being down)
- `make pg-up` + `make pg-migrate` (Postgres) — or fall back to the default SQLite
- S3 = the `HCP_ENDPOINT` already in `.env`
- LanceDB tables on that S3 (`make search-index` / `catalog-index` to populate)

New tooling:
- `Procfile.micro` — one line per service (uvicorn). Each command sets
  `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (the documented Ray/uv gotcha) and uses
  `uv run --no-sync`; the orchestrator line adds `RASK_ORCHESTRATOR_AUTOSTART=1`.
- `honcho` added to the root `dev` dependency group.
- `make dev-micro` → `uv sync --all-packages` then `honcho start -f Procfile.micro`.
  honcho auto-loads `.env`, so HCP/S3 creds and `RASK_VIEWER_INPUT/OUTPUT` flow in.

The frontend is untouched: `make viewer-frontend`'s Vite proxy already targets
`:8888`, which is now the gateway.

## Workspace wiring

- Root `pyproject.toml` `[tool.uv.workspace] members` gains
  `components/services/backends`.
- `projects/backends/pyproject.toml` composition lists `backends`, `viewer`,
  `storage`.
- `package.json` is **not** touched (its `workspaces` are JS-only).

## Verification

- `uv sync --all-packages` resolves with the new brick.
- Start `core-api` alone → `GET :8801/api/v1/health` returns ok.
- Start `gateway` + `core-api` → `GET :8888/api/v1/health` is proxied to core-api
  (proves routing) — works with **no** external deps (health needs only settings;
  Lance/S3/DB are tolerant).
- `make dev-micro` brings all six up; `make viewer-frontend` loads and the SPA
  functions through the gateway.

## Out of scope

- NATS JetStream / event-driven orchestrator (stays the timer loop).
- `packages/batchstate` physical extraction (services import `viewer` directly).
- Docker images, Helm changes for the new services, auth.
- Promoting each service module to its own Polylith brick.
