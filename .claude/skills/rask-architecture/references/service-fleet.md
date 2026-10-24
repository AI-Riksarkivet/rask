# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`) is SIX processes: compute `:8804` + flows
`:8840` + notifications `:8850` + the explorer trio — viewer `:8101`, search `:8102`, annotator `:8103`.
(`PORT_OFFSET=<n>` shifts all six so a second fleet can share a host.) It starts no router: the public
route table is the chart's HTTPRoutes, served by a Gateway (`rask-services-fleet`). In-cluster compute
ships in its own fixture chart (`deploy/fixtures/compute`), not the product chart, and publishes
`/api/ray` and `/api/serve` with its own HTTPRoute; the annotator likewise ships in its own client chart
(`deploy/fixtures/annotator`, ADR 0171) and publishes `/api/explorer/annotations` with its own HTTPRoute.

The zones' Vite `/api` proxies are per zone: `compute`/`studio`/`models` → `VIEWER_BACKEND` (`:8888`),
`home`/`lakehouse` → `LANCE_BACKEND` (`:8001`, which this fleet does NOT start), and `explorer`/`annotator`
have no `/api` proxy — they reach `:8101`/`:8102`/`:8103` through their own BFF. Their SSR reads one
`RASK_EDGE_URL` (default `http://localhost:8888`). An edge on :8888 comes from `make dev-frontends-k3s`
(k3s Traefik's Gateway, port-forwarded). See `rask-frontend`.

## Nine HTTP entrypoints are thin shells over one of three factories

`compute`, `flows` and `notifications` import routers (+ maybe a
lifespan) from a domain package and call `service_kit.make_service_app` — no business logic in
the entrypoint. Count by CALL SITE, not by import.

THE OTHER SIX COME OUT OF A FACTORY TOO, because copied boots drift: each repeated the same five-step
boot in copied comments (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit"
DUP-12), and one lost its request-id layer in the copying. `viewer`, `search` and `annotator` build through
`client_kit.media.app.build_media_app` over one shared `client_kit.media.lifespan`; `catalog`, `lineage` and
`maintenance` build through `service_kit.lance_app.build_lance_service_app`. Both still read their own
`core/config.py` settings and keep the `api/v1/endpoints/` layout — the factory owns the boot, not
the layout. See rask-architecture's "Three factories are sanctioned".

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `compute` | 8804 | `ray-kit` | `compute.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API. Its fixture chart's own HTTPRoute publishes `/api/ray` and `/api/serve`; deployed by that fixture chart |
| `viewer` (explorer plane) | 8101 | `client-kit` + `service-kit[governed,lancekit]` + `storage` | own (lazy registry) | no | public `/api/explorer/*`, rewritten to `/api/*` by the route, incl. the S3 objects browser |

## The lakehouse runs no ingest, no stage and no engine

The lakehouse runs no ingest of its own (ADR 0154): data enters only
through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
`DeclareTable` + vended credentials, `RegisterTable`), and ingest of an external source is a
bring-your-own client job (a sealed runner, CP-061) that commits through the catalog.

**The lakehouse runs no stage, no training and no engine (ADR 0155).** Moving data between tiers is the
user's pipeline, on their own compute and orchestrator, writing each tier through the catalog's doors.
The catalog announces every committed write as an OpenLineage `COMPLETE` on lineage, and the NATS
topics stay internal. A pipeline follows committed writes on lineage's durable `GET /events`, which
follows forward from a `since=` cursor in the commit-event shape `docs/COMMIT-EVENTS.md` documents (ADR 0156 C7,
LH-472). Lineage keeps that feed as a Lance table in the catalog's provenance namespace and answers no graph
query; the graph is bring-your-own, fed by its forwarder (ADR 0158).

## Why the fleet services never grow heavy deps

`compute` depends only on `service-kit` + `ray-kit` — **no DB**. `service-kit`'s
core stays dependency-light (no `ray`/`sqlmodel`; lance deps live behind the
`[lancekit]` extra): pulling a heavy dep into the core would force it onto every service and every test
that imports the factory. What only a client needs (lancedb, slowapi) is in `client-kit`, which no
lakehouse service installs.

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
