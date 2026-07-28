# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`):
gateway `:8888` + ray `:8804` + controlplane `:8820` + the media viewer `:8101`.
The frontend's Vite proxy targets `:8888` (the gateway). The orchestrator
process (`:8810`) died at P7a; core-api/search-api/volumes-api died in the
R6/R20 media wave (lance-ns-merge.md P7).

## Every HTTP entrypoint is a thin shell over `make_service_app`

Each `services/<svc>/src/<svc>/__init__.py` imports routers (+ maybe a
lifespan) from a domain package and calls `service_kit.make_service_app`. No business
logic in the entrypoint.

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first, **no catch-all**; upstreams env-overridable (`RASK_RAY_URL`, `RASK_CONTROLPLANE_URL`, `RASK_MEDIA_*_URL`, …). |
| `ray_api` (the `ray` service) | 8804 | `ray-kit` | `ray_api.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |
| `viewer` (media plane) | 8101 | `service-kit[lancekit]` + `storage` | own (lazy registry) | no | `/api/media/*` incl. the S3 objects browser ported from volumes-api |

## The batches/orchestrator plane is gone (P7a)

The reconcile→derive→submit loop, the two-lane prefetch/htr slot model, the
`batches` table and S3-sync were deleted at the compute-plane cutover
(lance-ns-merge.md P7a). The pipeline head is the medallion producer's
`POST /ingest-iiif` (IIIF → raw page-image Lance dataset, ONE raw-write
OpenLineage event); `/raw-arrival` fires the `medallion.raw` cascade, and the
HTR stages run as event-triggered movers on the unified Ray cluster (P7b).

## Why the fleet services never grow heavy deps

`ray_api` depends only on `service-kit` + `ray-kit` — **no DB**. `service-kit`'s
core stays dependency-light (no `ray`/`sqlmodel`; lance deps live behind the
`[lancekit]` extra the media plane opts into): pulling a heavy dep into the
core would force it onto every service and every test that imports the factory.

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
