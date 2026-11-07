# The services: ports, entrypoints, and which package each composes

Three Python services live in `services/`, the lakehouse's — `catalog` `:2333`, `lineage` `:8000`, `maintenance`
`:8000`, all run from ONE image (`.docker/rest-catalog.dockerfile`) with different commands. No process routes: the
public route table is the product chart's HTTPRoutes, served by the platform's Gateway (`rask-services-fleet`).

## Every entrypoint is a thin shell over one factory

`catalog` (`catalog.main`), `lineage` (`lineage.main`) and `maintenance` (`maintenance.service`) build through
`lakehouse_kit.lance_app.build_lance_service_app`, because copied boots drift: each once repeated the same five-step
boot in copied comments (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit"
DUP-12), and one lost its request-id layer in the copying. They still read their own `core/config.py` settings and
keep the `api/v1/endpoints/` layout — the factory owns the boot, not the layout.

| Service | Port | Composes package(s) | Factory | DB? | Notes |
|---|---|---|---|---|---|
| `catalog` | 2333 | `lakehouse-kit[data]`, `lineage-kit`, `service-kit[governed,arrow]` | `build_lance_service_app` | no | the Lance Namespace doors and the management doors; public `/api/catalog` |
| `lineage` | 8000 | `lakehouse-kit[data]`, `lineage-kit`, `service-kit[governed,arrow]` | `build_lance_service_app` | no | intake, commit feed, forwarder (ADR 0158); public `/api/lineage` |
| `maintenance` | 8000 | `lakehouse-kit[data]`, `lineage-kit`, `service-kit[governed,arrow]` | `build_lance_service_app` | no | sweep, reconciler, due list; no public row |

## The lakehouse runs no ingest, no stage and no engine

The lakehouse runs no ingest of its own (ADR 0154): data enters only through the catalog's Lance Namespace doors
(`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`), and
ingest of an external source is a bring-your-own client job that commits through the catalog.

**The lakehouse runs no stage, no training and no engine (ADR 0155).** Moving data between tiers is the user's
pipeline, on their own compute and orchestrator, writing each tier through the catalog's doors. The catalog announces
every committed write as an OpenLineage `COMPLETE` on lineage, and the NATS topics stay internal. A pipeline follows
committed writes on lineage's durable `GET /events`, which follows forward from a `since=` cursor in the commit-event
shape `docs/COMMIT-EVENTS.md` documents (ADR 0156 C7, LH-472). Lineage keeps that feed as a Lance table in the
catalog's provenance namespace and answers no graph query; the graph is bring-your-own, fed by its forwarder (ADR 0158).

## Why the platform package never grows heavy deps

`service-kit`'s base stays dependency-light (no `ray`, `lancedb` or `sqlmodel`; the auth plane behind `[governed]`, the
Arrow guards behind `[arrow]`): it is what a client pins by lakehouse tag (XC-186), and pulling a heavy dependency into
its base forces it onto every client and every test that imports it. The lakehouse's Lance dependencies live
in `lakehouse-kit[data]`, which no client installs.

## Lifespan injection recap

`build_lance_service_app(lifespan=...)` takes the service's own `Callable[[FastAPI], AbstractAsyncContextManager[None]]`,
and it is required. The lifespan opens the service's clients on enter and tears them down on exit, sets
`app.state.startup_complete` once they are open and `app.state.shutting_down` on the way out (the flags `/readyz` reads,
`service_kit.lifecycle`), and registers anything it hosts there, never at import.
