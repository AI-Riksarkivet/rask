# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`) is SIX processes:
gateway `:8888` + compute `:8804` + controlplane `:8820` + the explorer trio —
viewer `:8101`, search `:8102`, annotator `:8103`. (`PORT_OFFSET=<n>` shifts all six
so a second fleet can share a host.) The orchestrator process (`:8810`) died at P7a;
core-api/search-api/volumes-api died in the R6/R20 media wave (lance-ns-merge.md P7).

The frontend's Vite proxy targets `:8888` (the gateway) for `compute`/`studio`/`train`
**only**; `home`/`lakehouse` proxy `/api` to `LANCE_BACKEND` (`:8001`, which this fleet
does NOT start) and `explorer`/`annotator` have no `/api` proxy at all — they reach
`:8101`/`:8102`/`:8103` through their own BFF. See `rask-frontend`.

## Twelve HTTP entrypoints are thin shells over one of three factories

`compute`, `controlplane`, `flows`, `ingest` and `notifications` import routers (+ maybe a
lifespan) from a domain package and call `service_kit.make_service_app` — no business logic in
the entrypoint. Count by CALL SITE, not by import: `gateway` names the factory in three comments
and calls it nowhere. `gateway` builds `FastAPI(...)` itself (it is a proxy, not a router host),
though it does call `service_kit.setup_otel` directly, so it is on the shared telemetry path
even while it is off the shared app-factory path.

THE OTHER SEVEN ALSO COME OUT OF A FACTORY NOW, and this paragraph used to say they did not —
that they "build `FastAPI(...)` in `main.py`/`service.py`/`producer.py`/`mover.py`". They did, all
seven, each repeating the same five-step boot in copied comments (docs/DECISIONS.md "The Python estate audit" DUP-12), until
the mover was found to have lost its request-id layer in the copying. Today: `viewer`, `search` and
`annotator` build through `service_kit.media.app.build_media_app` over one shared
`service_kit.media.lifespan`; `catalog`, `lineage`, the two `medallion` apps and `maintenance` build
through `service_kit.lance_app.build_lance_service_app`. Both still read their own
`core/config.py` settings and keep the `api/v1/endpoints/` layout — the factory owns the boot, not
the layout. See rask-architecture's "Three factories are sanctioned".

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first, **no catch-all**; upstreams env-overridable (`RASK_COMPUTE_URL`, `RASK_CONTROLPLANE_URL`, `RASK_CATALOG_API_URL`, `RASK_LINEAGE_API_URL`, `RASK_MEDALLION_API_URL`, `RASK_INGEST_URL`, `RASK_EXPLORER_{VIEWER,SEARCH,ANNOTATOR}_URL`). There is **no** `RASK_MEDIA_*_URL` — the media→explorer rename took those names with it. |
| `compute` | 8804 | `ray-kit` | `compute.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |
| `viewer` (explorer plane) | 8101 | `service-kit[lancekit]` + `storage` | own (lazy registry) | no | `/api/explorer/*` incl. the S3 objects browser ported from volumes-api |

## The batches/orchestrator plane is gone (P7a)

The reconcile→derive→submit loop, the two-lane prefetch/compute slot model, the
`batches` table and S3-sync were deleted at the compute-plane cutover
(lance-ns-merge.md P7a). The pipeline head is now the **ingest plane**
(`services/ingest`, `:8830`, `.docker/ingest.dockerfile`): `POST /api/ingests` takes a
source-agnostic `SourceSpec` — `iiif`, `s3-prefix`, `local-dir`, registered in
`ingest/adapters.py`, each declaring its own external lineage input (R23: raw is the
external world, bronze the first governed tier) — a Dapr Workflow fans units onto a NATS
JetStream work queue, and the lander commits ONE Lance version through the **catalog**
(`bronze$pages`). It is the CATALOG's own COMPLETE event, not an ingest-side emit, that
announces the write and that `/bronze-arrival` filters on. The medallion producer keeps the events lane
(`POST /produce` → `bronze$events`) and the cascade head `/bronze-arrival`; **its
`POST /ingest-iiif` is deleted, and so is the gateway row** — verified 2026-08-15 against
`gateway/__init__.py::_routes()`, which carries no such row; the only `ingest-iiif` text left in
that file is the comment explaining why the row's absence is safe (`_pick_route` requires
`path == prefix` or `prefix + "/"`, and the next character is `-`). This reference previously
described the row as a surviving deprecation shim, which would have made a call there **502
rather than 404** — naming a backend as broken instead of the path as absent. The producer's INGEST
doors are exactly `POST /produce`, `POST /ingest-media` and `POST /train`, all root-mounted and
token-guarded — that rule bounds what may LAND data, and adding a protocol-specific fourth would make
that protocol privileged. It does not bound the router surface, which is six: those three plus
`promotions` (the quality gate's third answer), `mover_ops` (`/movers/*`, workflow terminate) and
`movers/stages/rerun` (the cascade's edge-addressed repair verb, 2026-09-04). Those three are human
control rather than ingest, which is why they do not weaken the rule. A workload's stages run as event-triggered movers on the unified
Ray cluster (P7b) — the cascade is modality-blind, so this is the same shape for every runner.

**Both bronze lanes converge on one topic, so movers must discriminate.** The events
lane (the producer's `/produce` → `bronze$events`) and the page lane (the ingest
plane's lander → `bronze$pages`, committed through the catalog) both end in a lineage
COMPLETE that `/bronze-arrival` turns into `medallion.bronze`, so every mover
subscribed to it sees both arrivals. The trigger carries the `dataset` that was
actually written (`ingest_trigger._bronze_write_dataset`) and `handle_stage` DROPs a
name that is not its own `from_dataset` — compared against the RAW setting, never the
project-qualified one, since the trigger is unqualified for every tenant. An ABSENT
`dataset` makes no claim and proceeds. Without that check a page arrival drove the
events mover to completion: a real write plus a COMPLETE attributed to the other lane's
token.

## Why the fleet services never grow heavy deps

`compute` depends only on `service-kit` + `ray-kit` — **no DB**. `service-kit`'s
core stays dependency-light (no `ray`/`sqlmodel`; lance deps live behind the
`[lancekit]` extra the explorer plane opts into): pulling a heavy dep into the
core would force it onto every service and every test that imports the factory.

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
