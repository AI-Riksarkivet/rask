---
name: rask-services-fleet
description: Gateway (:8888) routing across the rask backend fleet — which service owns an `/api/*` prefix, plus ports and `RASK_*_URL` overrides for compute, controlplane, ingest and the explorer trio (viewer/search/annotator). Use when an `/api/*` call returns `404 no upstream`, `502 upstream unreachable` or a 403; when adding or moving an endpoint or a gateway route row; when changing a port, `RASK_API_PREFIX` or a `RASK_*_URL`; or when reading `scripts/dev-micro.sh`.
---

# rask services fleet (gateway + per-domain backends)

The day-to-day backend map. The **gateway** on `:8888` is a stateless reverse proxy that path-routes `/api/*` to per-domain services. The old `viewer` monolith is gone; the batches/orchestrator plane died at P7a; and the **R6/R20 media wave (2026-07-28) retired core-api, search-api, and volumes-api** — the S3 object browser now lives in the **explorer viewer** (`/api/explorer/object*`), and lines/EAD FTS re-land as catalog-governed Lance tables behind `/api/explorer/search` (docs/architecture/lance-ns-merge.md). `scripts/dev-micro.sh` is the source of truth for the process list + ports.

⚠️ **The frontend's dev proxy is per-zone and inconsistent** — there is no single "the SPA targets :8888":

| Zones | `/api` proxies to |
|---|---|
| `compute`, `studio`, `train` | `VIEWER_BACKEND` → `:8888`, the gateway |
| `home`, `lakehouse` | `LANCE_BACKEND` → **`:8001`**, the lineage service — and nothing in `dev-micro.sh` serves `:8001` |
| `explorer`, `annotator` | no `/api` proxy at all; they reach `:8101`/`:8102`/`:8103` through their own BFF |

So a `/api/*` call that works in `compute` can 404 or hang in `lakehouse`. See `rask-frontend` for the matching SSR base-URL split.

For FastAPI app/router/lifespan idioms see `fastapi`. This skill is *only* the topology + invariants.

## When to use

- Adding or moving an endpoint — pick the owning service and confirm the gateway prefix routes to it.
- Debugging a `404 no upstream` or `502 upstream unreachable` seen through the SPA.
- Changing a port or pointing the gateway at a remote backend via `RASK_*_URL`.
- Reading/editing `scripts/dev-micro.sh` or wiring a new service into the fleet.

## Fixed port map + env overrides

`scripts/dev-micro.sh` exports `*_PORT` defaults; the gateway reads `RASK_*_URL` (localhost defaults below) so you can point it at remote/containerized backends without touching code.

| Service | Port | Gateway override env | Lifespan builds |
|---|---|---|---|
| **gateway** | 8888 | — (it *is* the proxy) | `httpx.AsyncClient` + route table only |
| **compute** | 8804 | `RASK_COMPUTE_URL` | dashboard httpx client + Ray Job SDK client |
| **controlplane** | 8820 | `RASK_CONTROLPLANE_URL` | k8s client (read-only Project CRs for the home picker) |
| **explorer viewer** | 8101 | `RASK_EXPLORER_VIEWER_URL` | lazy `DatasetRegistry`; the S3 object browser builds its client **per store** from the catalog's storage registry (`RASK_STORES`) — a store declaring a `secret` gets those creds from the Dapr secret store, fail-closed and `lru_cache`d, never the process env (the old env-only `s3_client()` read the external raw tier against the warehouse and listed it as empty). FGA-gated since #90 — see invariant 10 |
| **explorer search** | 8102 | `RASK_EXPLORER_SEARCH_URL` | descriptor-driven Lance search |
| **annotator** | 8103 | `RASK_EXPLORER_ANNOTATOR_URL` | annotations plane |
| **ingest** | 8830 | `RASK_INGEST_URL` | the pre-bronze acquisition plane (control API + workers + the lander) — **`dev-micro.sh` does NOT start it**, so `/api/ingest/*` answers `502 upstream unreachable` against the local fleet |

The gateway also carries the lakehouse rows (`/api/catalog`, `/api/lineage`, `/api/produce`, `/api/train`) plus the **ingest** row `/api/ingest` → the ingest plane (`RASK_INGEST_URL`, `:8830`) — see `gateway/__init__.py::_routes()`. Two traps in that row. It rewrites to **`/api`, not `/v1`**: the ingest module's own docstrings say `/v1/ingests`, which is the ROUTER's path *before* `make_service_app` prepends `settings.api_prefix`, and the `/v1` version that shipped 404'd every call through the gateway. And `/api/ingest-iiif` is a **DEPRECATED** sibling pointing at the medallion's IIIF head, kept one window so the frontend can move to `/api/ingest` first. They are siblings, not nested: `_pick_route` requires `path == prefix or path.startswith(prefix + "/")`, so `/api/ingest-iiif` can never match the `/api/ingest` row (the next char is `-`, not `/`) — `tests/test_routing.py` pins both facts.

## Load-bearing invariants

1. **No fleet service owns relational state.** The batches table + Alembic lineage were deleted at P7a; the only databases left are the chart-managed lineage (AGE) and OpenFGA stores, owned by the lance services. Never add a DB engine to a fleet lifespan.
2. **Each service builds only its own `app.state` subset in its own lifespan.** The compute service opens only the dashboard/job clients. Don't widen a lifespan to grab resources the service doesn't use.
3. **Longest-prefix-first routing, NO catch-all.** `gateway/__init__.py::_routes()` returns prefixes most-specific-first; `_pick_route` returns the first whose `path == prefix or path.startswith(prefix + "/")`. Order: the deep explorer rows (`/api/explorer/search`, `/api/explorer/annotations`) before `/api/explorer`, then the lakehouse rows, then `{prefix}/ray`, `{prefix}/projects`, `/api/serve`. **There is no bare `/api` row since R6/R20** — an unmatched `/api/*` 404s with `no upstream`. A new public prefix needs its own route row.
4. **`/api/serve` and `/api/ray` both go to the compute service** (the URL namespace names the Ray cluster, not the service — R22: the SERVICE is `compute` on every surface — uv member, import, k8s/dapr/image — while the public paths stay `/api/ray` + `/api/serve`), but for different reasons: domain routers mount under `RASK_API_PREFIX` (`/api/v1`), while its `proxy_router` mounts at the **root** (no prefix) so `/api/serve/*` reaches the Ray Serve status API. Routers vs proxy_router is the `make_service_app` distinction.
5. **502 contract.** On `httpx.RequestError` (upstream not started / crashed / wrong port) the gateway raises `HTTPException(502, "upstream ... unreachable")` — a clean 502, never a 500 traceback. An unmatched path is a `404 no upstream`. Use the 502 to tell "backend down" from "wrong route."
6. **Hop-by-hop headers are stripped both ways** (`_HOP_BY_HOP`: connection, keep-alive, te, trailers, transfer-encoding, upgrade, host, proxy-*) per RFC 7230 §6.1. Responses stream back via `StreamingResponse(aiter_raw(), background=aclose)`. Don't re-add `Host`/`Transfer-Encoding`.
7. **Merged `/docs`.** The gateway intercepts `{prefix}/openapi.json` and `{prefix}/docs` itself: `_merged_openapi` fans out to every distinct upstream's `openapi.json` and merges `paths`+`components.schemas` into one spec, **skipping unreachable backends** (logged, not fatal). So the gateway's `/docs` shows the whole fleet.
8. **The storage browser's chain is BFF-shaped:** lakehouse zone `/lakehouse/api/explorer/*` (SvelteKit route) → gateway `/api/explorer/*` → viewer `/api/*` (`/api/explorer/objects` → `/api/objects`). Dev needs the viewer running (`dev-micro.sh` starts it); in-cluster it needs `explorer.enabled` + the viewer's rustfs netpol allowlist entry — **and, with `auth.enabled`, a bearer whose subject holds `can_browse_storage` on `LANCE_FGA_ROOT_OBJECT`** (owner/estate tier, deliberately not per-store: the shipped default stores come from `DEFAULT_STORES` in code and would never get tuples). `chart/templates/explorer.yaml` sets `LANCE_OIDC_*` + `LANCE_FGA_*` for **all three** explorer services — it was `if and (eq $name "annotator") auth.enabled`, so the viewer streamed page images and browsed S3 wide open on an auth-enabled estate; the vars change behaviour only where a route declares an auth dependency, so `search` is unaffected. The lakehouse proxy forwards the signed-in user's bearer but does not `requireSession`, so an anonymous browse arrives credential-less and is denied at the viewer, not at the BFF. Dev stays open — `LANCE_FGA_ENABLED` unset ⇒ the checker is permissive by construction.
9. **Paths are canonicalized before matching.** `_normalize_path` (`gateway/__init__.py`) collapses `.`, `..`, and duplicate slashes, preserving a trailing slash — so `..`/`//` variants can neither dodge the 403 blocklist nor slip past a longer prefix into a shorter one. This replaced nginx's `merge_slashes` + URI normalization.
10. **A 403 has two possible authors, and only one of them is the gateway.** The gateway's own 403 is `lineage_sidecar_guard` (`gateway/__init__.py`), which prefix-matches the **normalized, case-folded** path against `_lineage_sidecar_only_routes()` and returns `403 {"detail": "sidecar-only lineage route: <route>"}` before the `/api/lineage` proxy runs — the nginx `lance.lineageSidecarOnlyRoutes` blocklist rewritten in Python, with the services' own app-api-token check still the load-bearing guard. Every OTHER 403 through the gateway is a **proxied FGA denial**: since #90 the viewer gates `/api/datasets` + `/api/pages` on `can_get_metadata`, `/api/page` (image bytes) on `can_read_data`, and `/api/object{,s,/download}` on `can_browse_storage` against `LANCE_FGA_ROOT_OBJECT` (`viewer/api/security.py`), and the annotator gates its task plane. `Authorization` is not hop-by-hop, so the gateway forwards the bearer the BFF attached untouched and the service is what verifies it. Read the `detail` to tell them apart: the sidecar guard names a *route*; an FGA denial reads `<subject> lacks <relation> on <object>`.

## Gotchas

- **`RASK_API_PREFIX`'s code default is `/api/v1`, and nothing uses it.** Every deployment sets `/api` (`chart/values.yaml` under `config:`; `scripts/dev-micro.sh`; `.env.example`). Leave it unset and `/api/ray` and `/api/projects` silently move to `/api/v1/...` — off the paths every frontend client hardcodes. Gateway routing is built from the same value, and it calls `load_dotenv()` so it reads the `.env` the services do; keep them in sync.
- **`scripts/dev-micro.sh` deliberately does NOT bash-source `.env`.** Each service loads it via `python-dotenv` so JSON-list settings like `RASK_CORS_ORIGINS=["..."]` parse correctly; bash sourcing strips the quotes. Export only vars *not* in `.env`.
