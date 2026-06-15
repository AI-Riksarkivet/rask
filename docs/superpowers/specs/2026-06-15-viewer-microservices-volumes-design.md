# Break viewer into real microservices — pilot: extract volumes-api

**Date:** 2026-06-15
**Status:** Approved (design), pending implementation plan
**Strategy:** Strangler extraction (approach A). This spec covers **only** the first cycle:
the shared-platform-library inversion + the `volumes-api` extraction. Remaining services
and the core/orchestrator DB-ownership decision are separate follow-up cycles.

## Problem

The earlier backends split made six services that are *thin facades* over `viewer`: each is a
~10-line composition that imports `viewer`'s routers and runs `viewer`'s shared lifespan. That
gives independent **processes** but not real microservices — every service still depends on the
whole `viewer` package (its DB models, Lance, Ray, all routers), so there is no code ownership,
no fault isolation, no independent release, and no per-service dependency set.

Goal: make each service own its own code + dependency set, severing the `viewer` dependency.
Do it incrementally (strangler), one service at a time, starting with the cleanest slice.

## Why volumes-api is the pilot

`volumes-api` is fully **stateless**: its routes use **no `app.state`/lifespan resources**. The
service builds `storage` sources on demand from `Settings` (S3/IIIF), with no Postgres, no
LanceDB, no Ray. What it pulls from `viewer` is only:

- **platform/infra:** `core.config.Settings`, `core.exceptions.{NotFoundError, ValidationError}`,
  the generic `get_settings` dependency, middleware, the app factory.
- **its own domain:** `api/v1/endpoints/volumes.py`, `services/volumes.py`,
  `schemas/page.py` (`PageEntry` — used **only** by volumes; verified).

So it can be cut clean with no cross-service calls and no shared database.

## Design

### 1. Grow `service-kit` into the shared platform library

Today `service-kit` imports *from* `viewer`. Invert it: move the pure-infra modules **out of
`viewer.core` into `service_kit`**, and make `viewer` depend on `service-kit`.

| Moves into `packages/service-kit/src/service_kit/` | From |
|---|---|
| `config.py` (`Settings`) | `viewer/core/config.py` |
| `exceptions.py` (`NotFoundError`, `ValidationError`, `ServiceUnavailableError`, `register_handlers`) | `viewer/core/exceptions.py` |
| `middleware.py` (`register_middleware`) | `viewer/core/middleware.py` |
| `dependencies.py` — **only** `get_settings` + `SettingsDep` | split out of `viewer/api/dependencies.py` |
| `make_service_app`, logging, composable lifespan (new) | already here / new |

`viewer` re-exports or imports these from `service_kit` so its own code and the four
not-yet-extracted facade bricks keep working unchanged.

**Hard constraint — keep `service-kit` dependency-light.** It must NOT import `lancedb`, `ray`,
or `sqlmodel`/`sqlalchemy`. Verified: `config.py`, `exceptions.py`, `middleware.py` are already
free of those. The heavy resource dependencies live only in the kitchen-sink
`viewer/api/dependencies.py` (`get_s3`, `get_lines_tbl`, `get_catalog_tbl`, `get_ray`, …) — those
are **not** moved; they stay with `viewer` / their owning services. `service-kit` deps become:
`fastapi`, `pydantic-settings`, `python-dotenv`, `storage`.

### 2. Composable lifespan (the one new abstraction)

Replace `viewer.core.lifespan.make_lifespan` (which unconditionally builds DB + Lance + Ray + S3,
and imports domain code — orchestrator loop, ray client) with a `service-kit` lifespan builder
that takes a list of resource-setup callables. Each service wires only what it needs:

```python
# service_kit
app = make_service_app(title="volumes-api", routers=[routes.router], resources=[])
```

`volumes-api` passes `resources=[]`; the lifespan just sets `settings` on `app.state` so
`get_settings` works. `viewer` (and later-extracted services) pass their own resource builders.
The domain-specific builders (orchestrator loop, ray client, lance tables, db engine) stay in
`viewer` for now and are passed into the builder — they are NOT moved into `service-kit`.

### 3. What `volumes-api` owns (severed from `viewer`)

```
components/services/volumes_api/src/volumes_api/
  __init__.py     # app = make_service_app(title="volumes-api", routers=[routes.router], resources=[])
  routes.py       # ← moved from viewer/api/v1/endpoints/volumes.py
  service.py      # ← moved from viewer/services/volumes.py
  schemas.py      # ← moved from viewer/schemas/page.py  (PageEntry)
```

`components/services/volumes_api/pyproject.toml` deps become **`service-kit` + `storage`** only
(drop `viewer`). The deployable `projects/volumes-api` updates its workspace members to drop
`viewer`.

### 4. What changes in `viewer`

- Depends on `service-kit` for the platform code; its `core/{config,exceptions,middleware}.py`
  become thin re-exports of `service_kit` (or are deleted and imports repointed) — chosen at
  plan time to minimize churn for the four remaining facades.
- `api/v1/router.py` **drops** `volumes.router`.
- `api/v1/endpoints/volumes.py`, `services/volumes.py`, `schemas/page.py` are **deleted**
  (moved to the brick).
- `api/dependencies.py` keeps the resource deps; `get_settings`/`SettingsDep` now come from
  `service_kit` (re-exported for the facades).

### 5. End-state dependency graph (after this cycle)

```
storage ── service-kit   (platform: config, exceptions, middleware, factory, get_settings, lifespan)
              ↑                          ↑
         volumes-api                 viewer (+ core_api / search_api / ray_api / orchestrator facades)
   (service-kit + storage only;
    NO viewer, NO lancedb/ray/sqlmodel)
```

### 6. Testing

- New `components/services/volumes_api/tests/` — the moved viewer volumes tests, rebuilt against
  `volumes-api`'s own app factory and driven with an FS/fake `storage` source (they already need
  no DB/Ray).
- `viewer` suite keeps passing minus the moved volumes test.
- `service-kit` infra (config/exceptions/middleware) covered by both `viewer` and `volumes-api`.
- Live check: the fleet still serves `/api/v1/volumes/{vol}/pages` (and image/alto) through the
  gateway, now from `volumes-api`.

## Verification

- `uv sync --all-packages` resolves; `volumes-api` shows **no** `lancedb`/`ray`/`sqlmodel` in its
  transitive set (e.g. `uv tree --package volumes-api`).
- Import-smoke: `import volumes_api` with no `viewer` import in its module graph.
- `uvx ty check` adds no new diagnostics; `uvx ruff check` clean.
- Full `not slow` suite green (run the project's way — keep `addopts`; do NOT use `-o addopts=""`,
  which drops `--import-mode=importlib`).
- Live: `curl :8888/api/v1/volumes/.../pages` → 200 via gateway → volumes-api.

## Non-goals (this cycle)

- No change to `core_api`, `search_api`, `ray_api`, `orchestrator` (stay facades over `viewer`).
- No change to runtime behaviour of the volumes endpoints.
- The shared-Postgres / DB-ownership decision (core + orchestrator) is explicitly deferred.

## Follow-up cycles

1. Extract `search_api` (owns search service + schemas + the Lance resource builder).
2. Extract `ray_api` (owns ray dashboard/proxy + the Ray resource builder).
3. **Decide DB ownership for `core_api` + `orchestrator`** (own design pass): shared DB-models
   package both depend on, vs. orchestrator-calls-core over HTTP, vs. keep them as one service.
   This is where the strangler ends and `viewer` either becomes the core-api domain or dissolves.
