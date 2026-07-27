# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`):
gateway `:8888` + core-api `:8801` + search `:8802` + volumes `:8803` +
ray `:8804` + controlplane `:8820`. The frontend's Vite proxy targets `:8888`
(the gateway). The orchestrator process (`:8810`) died at P7a — HTR runs as
event-driven cascade compute on the lakehouse (lance-ns-merge.md P7).

## Every HTTP entrypoint is a thin shell over `make_service_app`

Each `services/<svc>/src/<svc>/__init__.py` imports routers (+ maybe a
lifespan) from a domain package and calls `service_kit.make_service_app`. No business
logic in the entrypoint.

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first; **does not import `service-kit` or any domain package** — pure forwarder. Upstreams env-overridable (`RASK_*_API_URL`). |
| `core_api` | 8801 | `core` | `core.lifespan.make_lifespan` | no | transitional husk: health + EAD `/catalog/search`; retires with the R6/R20 media wave |
| `volumes_api` | 8803 | `storage` only | default (stateless) | no | S3/IIIF image+ALTO proxy; builds storage sources on demand |
| `search_api` | 8802 | `storage` + `lancedb` | `search_api.lifespan.make_lifespan` | no | Lance `lines` FTS + S3 thumbnails (lines-only lifespan) |
| `ray_api` | 8804 | `ray-kit` | `ray_api.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |

## The batches/orchestrator plane is gone (P7a)

The reconcile→derive→submit loop, the two-lane prefetch/htr slot model, the
`batches` table and S3-sync were deleted at the compute-plane cutover
(lance-ns-merge.md P7a). The pipeline head is the medallion producer's
`POST /ingest-iiif` (IIIF → raw page-image Lance dataset, ONE raw-write
OpenLineage event); `/raw-arrival` fires the `medallion.raw` cascade, and the
HTR stages run as event-triggered movers on the unified Ray cluster (P7b).

## Why the viewer-free services never touch `core`

`volumes_api` / `search_api` / `ray_api` depend only on `service-kit` + their own
libs — **no `core`, no DB**. That's why `service-kit` must stay dependency-light
(no `lancedb`/`ray`/`sqlmodel`): pulling a heavy dep into `service-kit` would
force it onto these stateless services and onto every test that imports the
factory. Heavy deps live in the package that needs them (`core` → lancedb,
`search_api` → lancedb, `ray-kit` → ray).

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
