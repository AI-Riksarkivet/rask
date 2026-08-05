"""gateway — the single API origin for the SPA.

Path-routes `/api/*` to the per-domain backends and streams responses back. This
is the frontend proxy target (`:8888`), so the SPA needs no changes. Does not
import `viewer` — it only forwards HTTP.

Routing is longest-prefix-first; upstreams are env-overridable with localhost
defaults that match `Procfile.micro`.

Carries the lance-ns rows since the gateway fold (docs/architecture/lance-ns-merge.md,
decision 4 + P1 "Gateway fold (code half)" + P4: rask's FastAPI gateway wins, the
nginx gateway retired): `/api/catalog`, `/api/lineage`, `/api/produce`, `/api/train`
and the whole-plane explorer namespace `/api/explorer{,/search,/annotations}`. The lance
services serve their own internal prefixes (`/v1/...`, `/api/...`), so each route
row names the upstream prefix that replaces the public one — a wrong prefix
silently 404s (the dev-micro.sh warning). The nginx `lance.lineageSidecarOnlyRoutes`
403-blocklist became the `lineage_sidecar_guard` middleware below.
"""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from service_kit import setup_otel
from service_kit.schemas.health import Liveness


log = logging.getLogger("gateway")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailers", b"transfer-encoding", b"upgrade", b"host"}
)

#: Trust headers a CLIENT must never be able to set. Stripped from every inbound request, on every
#: route, before it is forwarded.
#:
#: Dapr stamps these on the way IN to a backend, and the estate's doors read them as proof of who is
#: calling. A client that sets one is asserting an identity, and the edge is the only place that
#: assertion can be refused — past here the value is indistinguishable from the sidecar's.
#:
#: MEASURED against the ingest door on the live cluster, AFTER it had already been fixed to refuse
#: public callers:
#:
#:     anonymous POST, no header                        -> 403
#:     anonymous POST + `dapr-caller-app-id: gateway`   -> 403
#:     anonymous POST + `dapr-caller-app-id: medallion` -> 202 ACCEPTED
#:
#: daprd APPENDS its own stamp rather than replacing a client's, and FastAPI's `Header()` binds the
#: FIRST occurrence — verified directly: `[medallion, gateway]` binds `medallion`, `[gateway,
#: medallion]` binds `gateway`. One forged header turned every caller-identity check in the estate
#: back into the bypass it was written to close.
#:
#: This belongs HERE and not in the doors: a door sees one value and cannot tell whose it is, while
#: the gateway knows for a fact that anything arriving on its public listener came from a client.
#: `dapr-api-token` is included for the same reason — a caller must never be able to present the
#: estate's service credential just by copying it into a header.
_CLIENT_SPOOFABLE = frozenset(
    {
        b"dapr-caller-app-id",
        b"dapr-api-token",
        b"dapr-app-id",
        # The lineage service door reads this as the caller's IDENTITY and checks it against an
        # allowlist (`lineage.api.security._service_principal`). Combined with the token daprd
        # stamps on the way in, a client that sets it names itself an allowlisted service — the
        # same laundering as the caller-app-id, one header along.
        b"x-lance-service-identity",
    }
)


def _rewrite_location(location: str) -> str:
    """Scrub an upstream redirect Location: an absolute URL (which carries the
    upstream's internal host, e.g. 127.0.0.1:8804) becomes path(+query) so the
    caller resolves it against the gateway origin instead of an unreachable
    in-cluster address."""
    u = httpx.URL(location)
    if u.host:
        rel = u.path
        if u.query:
            rel += "?" + u.query.decode()
        return rel
    return location


Route = tuple[str, str, str, str]
"""(public path-prefix, upstream path-prefix, dapr app-id, httpx fallback URL).

The upstream prefix REPLACES the public one when forwarding. rask rows keep it
identical (the fleet services mount under RASK_API_PREFIX themselves); lance rows
rewrite — catalog/lineage serve `/v1/...`/`/runs` at root, the medallion producer
serves `/produce`+`/ingest-iiif`+`/train` at root, and the media trio serves
`/api/...` internally.
"""


def _routes() -> list[Route]:
    # Mirror RASK_API_PREFIX so routing lines up with where the rask endpoints
    # mount. load_dotenv() so the gateway sees the same .env config the backends do.
    load_dotenv()
    prefix = os.environ.get("RASK_API_PREFIX", "/api/v1").rstrip("/")
    # The Ray-plane service is `compute` (R22) — dapr app-id `compute`, upstream
    # RASK_COMPUTE_URL. Its PUBLIC rows stay /api/ray + /api/serve: the URL
    # namespace names the Ray cluster those endpoints introspect/proxy, not the
    # service that serves them, so renaming the service does not move the paths.
    compute = ("compute", os.environ.get("RASK_COMPUTE_URL", "http://127.0.0.1:8804"))
    controlplane = ("controlplane", os.environ.get("RASK_CONTROLPLANE_URL", "http://127.0.0.1:8820"))
    # lance-plane upstreams (P1 gateway fold). Localhost defaults follow the lance
    # dev conventions: catalog 2333, lineage 8000, the lance-ray producer 8002 (the
    # port-forward the verify/e2e scripts use — its in-cluster port 8000 collides
    # with lineage on one host), explorer trio 8101/8102/8103 (chart explorer.services).
    catalog = ("catalog", os.environ.get("RASK_CATALOG_API_URL", "http://127.0.0.1:2333"))
    lineage = ("lineage", os.environ.get("RASK_LINEAGE_API_URL", "http://127.0.0.1:8000"))
    medallion = ("lance-ray", os.environ.get("RASK_MEDALLION_API_URL", "http://127.0.0.1:8002"))
    viewer = ("viewer", os.environ.get("RASK_EXPLORER_VIEWER_URL", "http://127.0.0.1:8101"))
    explorer_search = ("search", os.environ.get("RASK_EXPLORER_SEARCH_URL", "http://127.0.0.1:8102"))
    annotator = ("annotator", os.environ.get("RASK_EXPLORER_ANNOTATOR_URL", "http://127.0.0.1:8103"))
    # The ingest plane (open_ingest.md Phase 1) — a BARE app-id, deliberately: the medallion row
    # below points at `lance-ray`, a legacy app-id that no longer names anything about the service
    # it reaches (audit m1). A new row does not inherit that mistake.
    ingest = ("ingest", os.environ.get("RASK_INGEST_URL", "http://127.0.0.1:8830"))
    # longest / most-specific prefixes first; the prefix itself is the catch-all.
    # The two deeper explorer rows MUST outrank /api/explorer. There is NO bare /api
    # catch-all since the R6/R20 wave (core-api/search-api/volumes-api retired):
    # an unmatched /api/* 404s at the gateway with "no upstream", which is correct.
    return [
        ("/api/explorer/search", "/api/search", *explorer_search),
        ("/api/explorer/annotations", "/api/annotations", *annotator),
        ("/api/explorer", "/api", *viewer),
        ("/api/catalog", "", *catalog),
        ("/api/lineage", "", *lineage),
        ("/api/produce", "/produce", *medallion),
        # /api/ingest must outrank /api/ingest-iiif is NOT true — they are siblings, not nested, and
        # `_pick_route` requires `path == prefix or path.startswith(prefix + "/")`, so
        # "/api/ingest-iiif" can never match the "/api/ingest" row (the next char is "-", not "/").
        # Stated because it looks like a longest-prefix hazard and is not; ordering them either way
        # is safe, and `tests/test_routing.py` pins that rather than leaving it to a reading.
        # Rewrites to "/api", not "/v1". The ingest service is composed by `make_service_app`, which
        # mounts its routers under `settings.api_prefix` — "/api" — so the pod serves `/api/ingests`
        # and the live openapi.json says so. The "/v1" here was taken from the module's own
        # docstrings, which describe the route as `/v1/ingests`; that is the ROUTER's path, before
        # the prefix the app factory adds. Every call through the gateway would have 404'd, and the
        # unit tests could not see it because they assert the gateway's rewrite in isolation rather
        # than against the service's real mount. Same shape as the `/api/media` row above.
        ("/api/ingest", "/api", *ingest),
        # DEPRECATED — the medallion's IIIF head. Retires with the nine-plus-three IIIF files
        # (A12); kept for one deprecation window so the frontend can move to /api/ingest first.
        ("/api/ingest-iiif", "/ingest-iiif", *medallion),
        ("/api/train", "/train", *medallion),
        (f"{prefix}/ray", f"{prefix}/ray", *compute),
        (f"{prefix}/projects", f"{prefix}/projects", *controlplane),
        ("/api/serve", "/api/serve", *compute),
    ]


def _pick_route(path: str, routes: list[Route]) -> Route | None:
    for route in routes:
        prefix = route[0]
        if path == prefix or path.startswith(prefix + "/"):
            return route
    return None


def _normalize_path(path: str) -> str:
    """Collapse dot-segments and duplicate slashes so `..`/`.`/`//` variants can
    neither dodge the 403 blocklist nor slip past a longer route prefix into a
    shorter one — the same canonicalization nginx applied (merge_slashes + URI
    normalization) before its regex 403s ran."""
    segments: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segments:
                segments.pop()
            continue
        segments.append(seg)
    normalized = "/" + "/".join(segments)
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def _lineage_sidecar_only_routes() -> tuple[str, ...]:
    """The Dapr-delivered lineage routes (pub/sub ingest + the cron reconcile
    binding) that must never be reachable through the public edge — proxying them
    would ride the gateway's own sidecar and stamp the trusted app-api-token.
    Ported from the retired nginx gateway's `lance.lineageSidecarOnlyRoutes` helm
    helper (lance-ns-merge.md P1); the chart renders the same one-source list into
    RASK_LINEAGE_SIDECAR_ONLY_ROUTES (comma-separated route-name prefixes)."""
    raw = os.environ.get("RASK_LINEAGE_SIDECAR_ONLY_ROUTES", "lineage-events,lineage-reconcile-cron")
    return tuple(r.strip().lower() for r in raw.split(",") if r.strip())


def _dapr_enabled() -> bool:
    return os.environ.get("RASK_DAPR_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _target_base(app_id: str, fallback_url: str) -> str:
    """Invoke base for an app: the local Dapr sidecar when enabled, else the
    direct httpx upstream URL. Append the request path to this to get the URL."""
    if _dapr_enabled():
        port = os.environ.get("DAPR_HTTP_PORT", "3500")
        return f"http://127.0.0.1:{port}/v1.0/invoke/{app_id}/method"
    return fallback_url


def _distinct_targets(routes: list[Route]) -> list[tuple[str, str]]:
    """Unique (app_id, fallback_url) pairs, first-seen order (for openapi merge)."""
    seen: dict[str, str] = {}
    for _, _, app_id, fallback in routes:
        seen.setdefault(app_id, fallback)
    return list(seen.items())


async def _merged_openapi(client: httpx.AsyncClient, prefix: str, targets: list[tuple[str, str]]) -> dict:
    """Fetch each backend's OpenAPI and merge into one spec so the gateway's
    /docs shows every service's endpoints. Unreachable backends are skipped."""
    merged: dict = {
        "openapi": "3.1.0",
        "info": {"title": "rask API (gateway)", "version": "0.1.0"},
        "paths": {},
        "components": {"schemas": {}},
    }
    for app_id, fallback in targets:
        base = _target_base(app_id, fallback)
        try:
            resp = await client.get(f"{base}{prefix}/openapi.json", timeout=10.0)
            resp.raise_for_status()
            spec = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(f"openapi fetch failed for {app_id}: {exc}")
            continue
        merged["openapi"] = spec.get("openapi", merged["openapi"])
        merged["paths"].update(spec.get("paths", {}))
        merged["components"]["schemas"].update(spec.get("components", {}).get("schemas", {}))
    return merged


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    app.state.routes = _routes()
    app.state.api_prefix = os.environ.get("RASK_API_PREFIX", "/api/v1").rstrip("/")
    for prefix, upstream_prefix, app_id, fallback in app.state.routes:
        log.info(f"route {prefix} -> {app_id} ({_target_base(app_id, fallback)}{upstream_prefix})")
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="gateway", version="0.1.0", lifespan=lifespan)

# Opt-in OTLP tracing (no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set). The
# gateway is the front door, so its spans are the root of every request trace;
# HTTPXClientInstrumentor propagates context to the upstream services.
setup_otel(app, service_name="gateway")


@app.middleware("http")
async def lineage_sidecar_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """403 the sidecar-only lineage routes at the edge (lance-ns-merge.md P1: the
    nginx `lance.lineageSidecarOnlyRoutes` blocklist, become Python). Prefix match
    on the NORMALIZED, case-folded path — so trailing-slash, sub-path, casing, and
    `..`-normalization variants can't fall through to the /api/lineage proxy, the
    exact variants the nginx case-insensitive regex covered. Defense-in-depth: the
    services' own app-api-token check is the load-bearing guard either way."""
    path = _normalize_path(request.url.path).lower()
    for route in _lineage_sidecar_only_routes():
        if path.startswith(f"/api/lineage/{route}"):
            return JSONResponse(status_code=403, content={"detail": f"sidecar-only lineage route: {route}"})
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> Liveness:
    """Gateway process liveness/readiness — served by the gateway itself, never
    proxied (it is not under /api), so it stays green even when no domain
    backends are deployed (e.g. a front-door-only install). Probing a proxied
    path like /api/health instead would 502 once its upstream is removed and
    take the gateway NotReady.

    The body is the estate-wide ``Liveness`` model, so the gateway's probe schema
    matches compute's/controlplane's ``/api/health`` and the lance plane's ``/livez``
    (the path differs by design — see above — the shape must not)."""
    return Liveness()


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request) -> Response:
    prefix: str = request.app.state.api_prefix
    client: httpx.AsyncClient = request.app.state.http

    # Serve a unified API page aggregating every backend's schema, instead of
    # proxying /docs + /openapi.json to core-api only.
    if request.url.path == f"{prefix}/openapi.json":
        return JSONResponse(await _merged_openapi(client, prefix, _distinct_targets(request.app.state.routes)))
    if request.url.path == f"{prefix}/docs":
        return get_swagger_ui_html(openapi_url=f"{prefix}/openapi.json", title="rask API (gateway)")

    norm_path = _normalize_path(request.url.path)
    picked = _pick_route(norm_path, request.app.state.routes)
    if picked is None:
        raise HTTPException(status_code=404, detail=f"no upstream for {request.url.path}")
    route_prefix, upstream_prefix, app_id, fallback = picked
    base = _target_base(app_id, fallback)

    # The upstream prefix replaces the public one: /api/catalog/v1/x → /v1/x,
    # /api/explorer/search?q → /api/search?q. rask rows rewrite to themselves.
    upstream_path = upstream_prefix + norm_path[len(route_prefix) :]
    url = httpx.URL(f"{base}{upstream_path}").copy_with(query=request.url.query.encode("utf-8") or None)
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in _HOP_BY_HOP and k.lower() not in _CLIENT_SPOOFABLE]
    upstream_req = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        # Upstream unreachable (not started yet, crashed, wrong port) — surface a
        # clean 502 rather than a 500 traceback.
        raise HTTPException(status_code=502, detail=f"upstream {base} unreachable: {exc}") from exc

    out_headers = {}
    for k, v in upstream_resp.headers.items():
        if k.lower().encode() in _HOP_BY_HOP:
            continue
        out_headers[k] = _rewrite_location(v) if k.lower() == "location" else v

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=out_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )
