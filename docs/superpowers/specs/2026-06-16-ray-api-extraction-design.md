# Extract ray-api into a real microservice (strangler cycle 3)

**Date:** 2026-06-16
**Status:** Approved (design), pending implementation plan
**Strategy:** Strangler extraction (approach A, continued). This spec covers **only** the
`ray-api` cut + a new shared `ray-kit` package. The `core-api`+`orchestrator` merge / `viewer`
dissolution is the separate **cycle 4** capstone (its own spec).

## Problem

After cycles 1–2, `ray-api` is still a thin facade: its `__init__.py` imports `viewer`'s
`ray`/`health` routers + `ray.proxy_router` and injects `viewer.core.lifespan.make_lifespan`
(which builds DB + Lance + Ray + S3 + the orchestrator task). It uses only the Ray dashboard
client + an HTTP client, yet drags in the whole `viewer` package (Postgres, Lance, sqlmodel).

Goal: sever `ray-api` from `viewer` so it owns its routes/service/proxy/health and builds only
the Ray client + HTTP client, depending on `service-kit` + a new `ray-kit` + `httpx` only.

## The wrinkle: Ray code is shared with the orchestrator (more than first thought)

Unlike volumes/search, Ray code is used by code that is **not** moving this cycle — viewer's
orchestrator and submission. Reading the actual imports:

- `viewer/core/lifespan.py` and `services/orchestrator/loop.py` call `build_client`.
- `services/submission.py` and `services/orchestrator/derive.py` use `RAY_TRANSIENT_ERRORS`.
- `services/orchestrator/derive.py` imports `RayJob` **and calls `ray_dashboard.list_jobs(...)`**
  (derive.py:171) to compute orchestrator state.

So it is **not** just the schemas/errors that are shared — the dashboard **service function
`list_jobs`** is too. Splitting `ray_dashboard.py` so that `list_jobs` stays importable by the
orchestrator while the rest lives in `ray-api` would be a messy hairline cut.

**Decision (revised):** make **`packages/ray-kit`** the whole Ray library — the schemas, the
shared errors, `build_client`, AND the entire `ray_dashboard` service module. Both `ray-api`
and `viewer` depend on `ray-kit`. `ray-api` becomes a **thin FastAPI shell** (routes + proxy +
lifespan + health) that calls `ray-kit` for all Ray work; it has no service module of its own.
This keeps the cohesive Ray-dashboard code in one place and gives the orchestrator a clean
`ray-kit` import for `build_client` / `list_jobs` / `RAY_TRANSIENT_ERRORS` / `RayJob`.

## Design

### 1. New `packages/ray-kit` (the whole Ray library, viewer-free)

```
packages/ray-kit/src/ray_kit/
  __init__.py   # re-export the public surface (schemas, RAY_TRANSIENT_ERRORS,
                #   build_client, and the dashboard fns: health, list_jobs, job_logs,
                #   cluster_status, list_actors, list_tasks, overview, logs, proxy)
  schemas.py    # ← moved VERBATIM from viewer/schemas/ray.py (RayJob, RayJobsPayload, RayHealth,
                #    RayNode, RayGpu, RayActor(s)Payload, RayTask(s)Payload, RayOverviewPayload,
                #    RayEvent, RayLogsPayload, RayJobLogsPayload, ProxyResponse, …). Pure
                #    pydantic + `ray.dashboard...JobStatus`; NO viewer imports → zero import changes.
  dashboard.py  # ← moved from viewer/services/ray_dashboard.py (build_client, RAY_TRANSIENT_ERRORS,
                #    health, list_jobs, job_logs, cluster_status, list_actors, list_tasks,
                #    overview, logs, proxy + helpers). Only import change:
                #    `from viewer.schemas.ray import (...)` → `from ray_kit.schemas import (...)`.
```

`ray-kit` deps: `pydantic`, `ray`, `requests`, `httpx`, `anyio`. No FastAPI, no viewer, no DB.
It is the cohesive Ray-dashboard/SDK library that both `ray-api` and the orchestrator import.

### 2. `ray-api` brick — thin FastAPI shell over `ray-kit` (severed from viewer)

```
components/services/ray_api/src/ray_api/
  __init__.py     # make_service_app(title="ray-api", routers=[health.router, routes.router],
                  #   proxy_router=proxy.router, lifespan=make_lifespan)
  lifespan.py     # make_lifespan(settings): build app.state.http (httpx.AsyncClient) +
                  #   app.state.ray_client (ray_kit.build_client, via to_thread); NO DB/Lance/S3
  dependencies.py # get_ray_client / get_http + RayClientDep / HttpDep
  routes.py       # ← viewer/api/v1/endpoints/ray.py's `router` (/ray: health, jobs, job logs,
                  #    cluster, actors, tasks, overview, logs); calls ray_kit.dashboard.*
  proxy.py        # ← viewer/api/v1/endpoints/ray.py's `proxy_router` (/api/serve/* proxy);
                  #    calls ray_kit.dashboard.proxy
  health.py       # own health router (same pattern as volumes_api/search_api)
components/services/ray_api/tests/{conftest.py,test_ray.py}  # offline smoke tests
```

`ray-api` deps become **`service-kit` + `ray-kit` + `httpx` + `uvicorn`** (drop `viewer`).
`ray` comes transitively via `ray-kit`. There is **no `service.py`** — all Ray logic lives in
`ray-kit`. routes.py/proxy.py import the dashboard fns from `ray_kit` and the deps from
`ray_api.dependencies`; `SettingsDep` stays from `service_kit.dependencies`. The endpoint/proxy
bodies are unchanged except those import repoints.

`ray-api/lifespan.py` builds only:
- `app.state.settings = settings`
- `app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)` (the dashboard query fns
  need it; closed on shutdown)
- `app.state.ray_client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)`
  (None-tolerant when the dashboard is unreachable)

### 3. What changes in `viewer` (it still runs the orchestrator until cycle 4)

- **Delete** `viewer/schemas/ray.py` and `viewer/services/ray_dashboard.py` (moved to ray-kit).
- **Delete** `viewer/api/v1/endpoints/ray.py` (moved to ray-api as routes.py + proxy.py).
- Repoint the remaining viewer importers to `ray-kit` (exact, from grep):
  - `viewer/services/orchestrator/derive.py`: `from viewer.schemas.ray import RayJob` →
    `from ray_kit.schemas import RayJob`; and `from viewer.services import ray_dashboard` →
    `from ray_kit import dashboard as ray_dashboard` (so `ray_dashboard.list_jobs` /
    `ray_dashboard.RAY_TRANSIENT_ERRORS` keep working unchanged).
  - `viewer/services/submission.py`: `from viewer.services.ray_dashboard import
    RAY_TRANSIENT_ERRORS` → `from ray_kit import RAY_TRANSIENT_ERRORS`.
  - `viewer/services/orchestrator/loop.py`: `from viewer.services.ray_dashboard import
    build_client` → `from ray_kit import build_client`.
  - `viewer/core/lifespan.py`: `from viewer.services.ray_dashboard import build_client as
    build_ray_client` → `from ray_kit import build_client as build_ray_client`.
  - (grep-guard for any other `viewer.schemas.ray` / `viewer.services.ray_dashboard` importer.)
- `viewer/api/v1/router.py`: remove `ray` from the endpoints import + drop `include_router(ray.router)`.
- `viewer/main.py`: remove `ray` from `from viewer.api.v1.endpoints import ray, spa` (keep `spa`)
  and drop the `app.include_router(ray.proxy_router)` line (main.py:91) — the monolith no longer
  proxies `/api/serve/*` (ray-api owns it).
- **Keep** `get_ray_client` / `RayClientDep` in `viewer/api/dependencies.py` — the orchestrator
  endpoint still uses them (they read `app.state.ray_client`, which viewer's lifespan still sets
  via `ray_kit.build_client`). Keep the `JobSubmissionClient` type import.
- `viewer` gains a dependency on `ray-kit` (it already depends on `ray`); `service-kit` is
  unchanged and still must NOT import ray/lancedb/sqlmodel.

### 4. Gateway

No change. `gateway/__init__.py` already routes `{prefix}/ray` → `RASK_RAY_API_URL` (default
`:8804`) and `/api/serve/*` to the same upstream. Behaviour parity holds.

### 5. End-state dependency graph (after this cycle)

```
                  ray-kit (schemas + dashboard service + errors + build_client; ray/pydantic/requests/httpx/anyio)
                  ↑                                   ↑
storage ── service-kit            ray-api (service-kit + ray-kit + httpx)        viewer (+ core/orchestrator facades)
   ↑           ↑   ↑                                                                  ↑ also depends on ray-kit
 volumes-api  search-api (+lancedb)                                          (still imports viewer)
```

### 6. Testing

- New `components/services/ray_api/tests/test_ray.py`: offline smoke tests via `TestClient` —
  with no reachable dashboard (`ray_client = None`, dashboard HTTP unreachable), the endpoints
  return their None-tolerant payloads (health reports down, jobs/cluster/actors degrade
  gracefully) rather than 500. Plus `/api/v1/health` → 200 (ray-api's own health router). Reuse
  the conftest env-pinning pattern from search-api (the app singleton builds at import; pin
  `RASK_VIEWER_INPUT`/`OUTPUT`/`RASK_API_PREFIX` before import). Assert the exact behaviours the
  current code produces when Ray is down (read the moved code to pin the expected status/JSON).
- `ray-kit` gets a tiny unit test (e.g. `build_client` returns `None` for an unreachable URL;
  schema round-trips) if cheap; otherwise it is covered via ray-api's tests.
- `viewer` suite stays green minus any moved ray test.

## Verification

- `uv sync --all-packages` resolves; `uv tree --package ray-api` shows **no** `viewer`, and **no**
  `lancedb`/`sqlmodel`/`sqlalchemy` (ray IS expected, via ray-kit).
- `grep -rn "import viewer\|from viewer" components/services/ray_api/src packages/ray-kit/src` → nothing.
- `grep -rn "viewer.schemas.ray\|viewer.services.ray_dashboard" components/services/viewer/src` → nothing
  (all repointed to ray_kit).
- Import-smoke: `import ray_api` with no `viewer` in its module graph; `import viewer.main` still OK.
- Full `not slow` suite green; `ty` no new diagnostics; `ruff` clean of new findings.
- Live: restart the fleet (do NOT touch Ray; never `make ray-down`), then
  `/api/ray/health`, `/api/ray/jobs`, `/api/serve/...` served through the gateway by the
  independent ray-api; viewer/core/orchestrator endpoints + the orchestrator loop still work.

## Workspace bookkeeping

Adding `packages/ray-kit` requires editing **both** root `pyproject.toml` `[tool.uv.workspace]
members` and root `package.json` `workspaces` (per CLAUDE.md: membership is explicit, never
globbed), plus `[tool.uv.sources]` entries where consumed.

## Non-goals (this cycle)

- No change to `core-api`/`orchestrator` behaviour; the orchestrator keeps running inside viewer.
- No DB/Lance/S3 changes.
- No Helm/k8s work.

## Follow-up

**Cycle 4 (capstone):** merge `core-api` + `orchestrator` into one DB-owning service and dissolve
the `viewer` package (its remaining models/db/alembic/submission/sync/orchestrator/endpoints move
into the core brick). Then the Helm per-service deployment cycle.
