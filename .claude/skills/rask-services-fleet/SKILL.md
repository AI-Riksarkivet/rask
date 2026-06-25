---
name: rask-services-fleet
description: The rask backend topology — gateway (:8888) reverse-proxy + per-domain services (core-api, orchestrator, search, volumes, ray) and how they're wired. Use when adding/moving an endpoint, debugging a 404/502 from the SPA, changing a port or RASK_*_URL override, deciding which process owns the batches table, or reading scripts/dev-micro.sh.
---

# rask services fleet (gateway + per-domain backends)

The day-to-day backend map. The SPA's Vite proxy targets `:8888`; in the fleet that's the **gateway**, a stateless reverse proxy that path-routes `/api/*` to per-domain services. The old `viewer` monolith is gone — its domain logic lives in the shared `core` brick, composed by two thin entrypoints (`core_api`, `orchestrator`). `scripts/dev-micro.sh` is the source of truth for the process list + ports.

For FastAPI app/router/lifespan idioms see `fastapi`; for the orchestrator loop's async-task lifecycle see `python-infrastructure`. This skill is *only* the topology + invariants.

## When to use

- Adding or moving an endpoint — pick the owning service and confirm the gateway prefix routes to it.
- Debugging a `404 no upstream` or `502 upstream unreachable` seen through the SPA.
- Changing a port or pointing the gateway at a remote backend via `RASK_*_URL`.
- Deciding which process may write the `batches` table (answer: only `core-api` + `orchestrator`).
- Reading/editing `scripts/dev-micro.sh` or wiring a new service into the fleet.

## Fixed port map + env overrides

`scripts/dev-micro.sh` exports `*_PORT` defaults; the gateway reads `RASK_*_URL` (localhost defaults below) so you can point it at remote/containerized backends without touching code.

| Service | Port | Gateway override env | Writes `batches`? | Lifespan builds |
|---|---|---|---|---|
| **gateway** | 8888 | — (it *is* the proxy) | no | `httpx.AsyncClient` + route table only |
| **core-api** | 8801 | `RASK_CORE_API_URL` | **yes** | DB engine+sessionmaker, S3, Lance catalog, ray client; loop **off** |
| **search-api** | 8802 | `RASK_SEARCH_API_URL` | no | Lance `lines` table + S3 only |
| **volumes-api** | 8803 | `RASK_VOLUMES_API_URL` | no | **nothing** — fully stateless (builds storage sources per-request) |
| **ray-api** | 8804 | `RASK_RAY_API_URL` | no | dashboard httpx client + Ray Job SDK client |
| **orchestrator** | 8810 | `RASK_ORCH_API_URL` | **yes** | same `core` lifespan as core-api; loop **on** |

`scripts/dev-micro.sh` forces `RASK_ORCHESTRATOR_AUTOSTART=false` on *every* service except `orchestrator` (which gets `ORCH_AUTOSTART`, default `true`) — so the reconcile→submit loop runs in **exactly one** process. Don't autostart it in core-api.

## Load-bearing invariants

1. **Data ownership.** Only `core-api` and `orchestrator` open a DB engine (both via `core/lifespan.py::make_lifespan` → `make_engine`/`make_sessionmaker`). They are two processes over *one* `core` brick sharing the `batches` table transactionally — not independent services. `search`/`volumes`/`ray` are **stateless readers with no DB**: never give them a session or have them write batch state.
2. **Each service builds only its own `app.state` subset in its own lifespan.** volumes-api passes no `lifespan` at all (stateless). search-api opens only `lines_tbl`+`s3`. ray-api opens only the dashboard/job clients. Don't widen a lifespan to grab resources the service doesn't use.
3. **Longest-prefix-first routing.** `gateway/__init__.py::_routes()` returns prefixes most-specific-first; `_pick_upstream` returns the first whose `path == prefix or path.startswith(prefix + "/")`. Order: `{prefix}/search`, `/volumes`, `/ray`, `/orchestrator` → their services; **`/api/serve` → ray-api**; then `{prefix}` and `/api` → **core (the catch-all)**. New domain prefixes must go *before* the core catch-all or core will swallow them.
4. **`/api/serve` and `/api/ray` both go to ray-api**, but for different reasons: domain routers mount under `RASK_API_PREFIX` (`/api/v1`), while ray-api's `proxy_router` mounts at the **root** (no prefix) so `/api/serve/*` reaches the Ray Serve status API. Routers vs proxy_router is the `make_service_app` distinction.
5. **502 contract.** On `httpx.RequestError` (upstream not started / crashed / wrong port) the gateway raises `HTTPException(502, "upstream ... unreachable")` — a clean 502, never a 500 traceback. An unmatched path is a `404 no upstream`. Use the 502 to tell "backend down" from "wrong route."
6. **Hop-by-hop headers are stripped both ways** (`_HOP_BY_HOP`: connection, keep-alive, te, trailers, transfer-encoding, upgrade, host, proxy-*) per RFC 7230 §6.1. Responses stream back via `StreamingResponse(aiter_raw(), background=aclose)`. Don't re-add `Host`/`Transfer-Encoding`.
7. **Merged `/docs`.** The gateway intercepts `{prefix}/openapi.json` and `{prefix}/docs` itself: `_merged_openapi` fans out to every distinct upstream's `openapi.json` and merges `paths`+`components.schemas` into one spec, **skipping unreachable backends** (logged, not fatal). So the gateway's `/docs` shows the whole fleet, not just core's.

## Gotchas

- **`RASK_API_PREFIX` must match the backends.** Gateway routing is built from `RASK_API_PREFIX` (default `/api/v1`); it calls `load_dotenv()` so it reads the same `.env` the services do. Change the prefix and the route table shifts with it — keep them in sync.
- **`scripts/dev-micro.sh` deliberately does NOT bash-source `.env`.** Each service loads it via `python-dotenv` so JSON-list settings like `RASK_CORS_ORIGINS=["..."]` parse correctly; bash sourcing strips the quotes. Export only vars *not* in `.env`.
- A `404 no upstream` through the gateway means the path didn't match any prefix — usually a new endpoint mounted under a prefix the gateway doesn't know yet (see invariant 3), not a missing route on the backend.
