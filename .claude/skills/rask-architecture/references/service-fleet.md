# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`) is SEVEN processes:
gateway `:8888` + compute `:8804` + flows `:8840` + notifications `:8850` + the explorer trio —
viewer `:8101`, search `:8102`, annotator `:8103`. (`PORT_OFFSET=<n>` shifts all seven
so a second fleet can share a host.) The gateway routes compute's `/api/ray` and `/api/serve` rows to
`RASK_COMPUTE_URL`, and in-cluster compute ships in its own fixture chart (`deploy/fixtures/compute`),
not the product chart.

The frontend's Vite proxy targets `:8888` (the gateway) for `compute`/`studio`/`models`
**only**; `home`/`lakehouse` proxy `/api` to `LANCE_BACKEND` (`:8001`, which this fleet
does NOT start) and `explorer`/`annotator` have no `/api` proxy at all — they reach
`:8101`/`:8102`/`:8103` through their own BFF. See `rask-frontend`.

## Ten HTTP entrypoints are thin shells over one of three factories

`compute`, `flows` and `notifications` import routers (+ maybe a
lifespan) from a domain package and call `service_kit.make_service_app` — no business logic in
the entrypoint. Count by CALL SITE, not by import: `gateway` names the factory in three comments
and calls it nowhere. `gateway` builds `FastAPI(...)` itself (it is a proxy, not a router host),
though it does call `service_kit.setup_otel` directly, so it is on the shared telemetry path
even while it is off the shared app-factory path.

THE OTHER SIX COME OUT OF A FACTORY TOO, because copied boots drift: each repeated the same five-step
boot in copied comments (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit"
DUP-12), and one lost its request-id layer in the copying. `viewer`, `search` and `annotator` build through
`service_kit.media.app.build_media_app` over one shared `service_kit.media.lifespan`; `catalog`, `lineage` and
`maintenance` build through `service_kit.lance_app.build_lance_service_app`. Both still read their own
`core/config.py` settings and keep the `api/v1/endpoints/` layout — the factory owns the boot, not
the layout. See rask-architecture's "Three factories are sanctioned".

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first, **no catch-all**; upstreams env-overridable (`RASK_COMPUTE_URL`, `RASK_FLOWS_URL`, `RASK_NOTIFICATIONS_URL`, `RASK_CATALOG_API_URL`, `RASK_LINEAGE_API_URL`, `RASK_EXPLORER_{VIEWER,SEARCH,ANNOTATOR}_URL`). There is **no** `RASK_MEDIA_*_URL` — the media→explorer rename took those names with it. |
| `compute` | 8804 | `ray-kit` | `compute.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API. The gateway's `/api/ray` and `/api/serve` rows reach it; deployed by its own fixture chart |
| `viewer` (explorer plane) | 8101 | `service-kit[lancekit]` + `storage` | own (lazy registry) | no | `/api/explorer/*` incl. the S3 objects browser ported from volumes-api |

## The lakehouse runs no ingest, no stage and no engine

The lakehouse runs no ingest of its own (ADR 0154): data enters only
through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
`DeclareTable` + vended credentials, `RegisterTable`), and ingest of an external source is a
bring-your-own client job (a sealed runner, CP-061) that commits through the catalog.

**The lakehouse runs no stage, no training and no engine (ADR 0155).** Moving data between tiers is the
user's pipeline, on their own compute and orchestrator, writing each tier through the catalog's doors.
The catalog announces every committed write as an OpenLineage `COMPLETE` on lineage, and the NATS
topics stay internal. A pipeline follows committed writes on lineage's durable `GET /events`, which
ADR 0156 C7 gives a forward `since=` cursor and a documented commit-event shape (LH-472, #598; today it
pages only backwards).

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
