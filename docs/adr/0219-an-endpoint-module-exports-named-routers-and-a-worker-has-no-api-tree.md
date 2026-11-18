# 0219. An endpoint module exports named routers, one list mounts them, and a worker has no api tree (2026-11-18)

**Status:** accepted. Source: LH-580 (#807), item S8 of the 2026-11-09 audit (merged in from LH-581, #808), decided by
the owner on 2026-11-18: "One router rule, worker exempt".

## Context

The catalog composed its routers three ways. Some endpoint modules exported one `router` carrying its own prefix.
Some exported an empty `router` and a prefixed `management_router`, which `api/v1/router.py` found with
`getattr(_module, "management_router", None)`. Some built several prefixed sub-routers and merged them by hand into a
`router` at the end of the file. A module that misspelled `management_router` mounted nothing and said nothing, and a
new module had three shapes to choose from. Lineage set one router's prefix where it was included
(`include_router(ingest.router, prefix="/api/v1")`), against the rule the FastAPI reference states: a router's
prefix belongs on the router.

The audit also read the three services' layouts as three conventions: the catalog and lineage have `main.py`,
`api/v1/endpoints`, and maintenance has `worker.py` and no `api/`. Measured at `c22209742`, maintenance serves no
route of its own: it is a worker that pulls tasks from the bus, and its only HTTP surface is the two probes the app
factory installs, served from a thread with no lifespan.

## Decision

1. **An endpoint module exports its routers by name, each carrying its own prefix.** In the catalog a `spec_router`
   serves the Lance Namespace spec's doors under `/v1`, and every other router (`management_router`, or one named
   for what it is addressed by: `table_router`, `namespace_router`, `projects_router`) serves rask's own doors under
   `/management/v1`. No module exports an empty router or an aggregate of its own routers.
2. **One explicit list mounts them** (`catalog/api/v1/router.py`, `lineage/api/v1/router.py`). There is no `getattr`
   and no prefix at `include_router`. A router the list does not name is mounted nowhere, and a name it gets wrong
   fails the import.
3. **A worker has no api tree.** Maintenance keeps `worker.py`, `runtime.py`, `core/` and `services/`. A service gets
   `api/` when it serves a door of its own, and not before.

## Consequences

- The mounted paths did not move: `make openapi` showed no diff in either service's document when the rule was
  applied, which is the proof for the catalog's 54 spec routes and its management routes.
- A test that builds an app from one module includes that module's named routers.
- The three router dependencies every catalog route passes (`authenticate`, `require_request_names`, `authorize`)
  stay on the parent router, so a route keeps its gate whichever named router carries it.
