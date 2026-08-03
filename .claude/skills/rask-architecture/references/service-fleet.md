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
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first, **no catch-all**; upstreams env-overridable (`RASK_COMPUTE_URL`, `RASK_CONTROLPLANE_URL`, `RASK_MEDIA_*_URL`, …). |
| `compute` | 8804 | `ray-kit` | `compute.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |
| `viewer` (media plane) | 8101 | `service-kit[lancekit]` + `storage` | own (lazy registry) | no | `/api/explorer/*` incl. the S3 objects browser ported from volumes-api |

## The batches/orchestrator plane is gone (P7a)

The reconcile→derive→submit loop, the two-lane prefetch/htr slot model, the
`batches` table and S3-sync were deleted at the compute-plane cutover
(lance-ns-merge.md P7a). The pipeline head is the medallion producer's
`POST /ingest-iiif` (IIIF → BRONZE page-image Lance dataset, ONE bronze-write
OpenLineage event with the external `iiif://…` input — R23: raw is the external
world, bronze the first governed tier); `/bronze-arrival` fires the
`medallion.bronze` cascade, and the
HTR stages run as event-triggered movers on the unified Ray cluster (P7b).

**Both ingest lanes share one topic, so movers must discriminate.** The events
lane (`bronze$events`) and the IIIF page lane (`bronze$pages`) both publish
`medallion.bronze`, so every mover subscribed to it sees both arrivals. The
trigger carries the `dataset` that was actually written
(`ingest_trigger._bronze_write_dataset`) and `handle_stage` DROPs a name that is
not its own `from_dataset` — compared against the RAW setting, never the
project-qualified one, since the trigger is unqualified for every tenant. An
ABSENT `dataset` makes no claim and proceeds. Without that check a page arrival
drove the events mover to completion: a real write plus a COMPLETE attributed to
the other lane's token.

## Why the fleet services never grow heavy deps

`compute` depends only on `service-kit` + `ray-kit` — **no DB**. `service-kit`'s
core stays dependency-light (no `ray`/`sqlmodel`; lance deps live behind the
`[lancekit]` extra the media plane opts into): pulling a heavy dep into the
core would force it onto every service and every test that imports the factory.

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
