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
from urllib.parse import quote, unquote

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
serves `/produce`+`/train` at root, and the media trio serves
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
    # dev conventions: catalog 2333, lineage 8000, the medallion-producer producer 8002 (the
    # port-forward the verify/e2e scripts use — its in-cluster port 8000 collides
    # with lineage on one host), explorer trio 8101/8102/8103 (chart explorer.services).
    catalog = ("catalog", os.environ.get("RASK_CATALOG_API_URL", "http://127.0.0.1:2333"))
    lineage = ("lineage", os.environ.get("RASK_LINEAGE_API_URL", "http://127.0.0.1:8000"))
    medallion = ("medallion-producer", os.environ.get("RASK_MEDALLION_API_URL", "http://127.0.0.1:8002"))
    viewer = ("viewer", os.environ.get("RASK_EXPLORER_VIEWER_URL", "http://127.0.0.1:8101"))
    explorer_search = ("search", os.environ.get("RASK_EXPLORER_SEARCH_URL", "http://127.0.0.1:8102"))
    annotator = ("annotator", os.environ.get("RASK_EXPLORER_ANNOTATOR_URL", "http://127.0.0.1:8103"))
    # The ingest plane (open_ingest.md Phase 1) — a BARE app-id, deliberately: the medallion row
    # below points at `medallion-producer`, a legacy app-id that no longer names anything about the service
    # it reaches (audit m1). A new row does not inherit that mistake.
    ingest = ("ingest", os.environ.get("RASK_INGEST_URL", "http://127.0.0.1:8830"))
    # The studio flow-builder's server half (open_studio_flows.md "Backend"): the node catalog, graph
    # validation, and run execution. Bare app-id, like `ingest` and for the same reason.
    flows = ("flows", os.environ.get("RASK_FLOWS_URL", "http://127.0.0.1:8840"))
    # The notification plane (open_notifications.md D2): the per-subject inbox behind the bell. Bare
    # app-id, like `ingest` and `flows`, and 8850 continues the 8830/8840 fleet run.
    notifications = ("notifications", os.environ.get("RASK_NOTIFICATIONS_URL", "http://127.0.0.1:8850"))
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
        # SIBLING PREFIXES, not nested. `_pick_route` requires `path == prefix or
        # path.startswith(prefix + "/")`, so a row like "/api/ingest-iiif" could never have matched
        # the "/api/ingest" row — the next character is "-", not "/". The deprecated `/api/ingest-iiif`
        # row is GONE (A12 deleted the medallion route it pointed at, so it 502'd rather than 404'd,
        # which is the worse failure: it names a backend as broken instead of the path as absent),
        # but the ORDERING PROPERTY it demonstrated is still load-bearing and still tested.
        ("/api/ingest", "/api", *ingest),
        # DEPRECATED — the medallion's IIIF head. Retires with the nine-plus-three IIIF files
        # (A12); kept for one deprecation window so the frontend can move to /api/ingest first.
        ("/api/train", "/train", *medallion),
        # The APPROVE door for a held promotion. It is on the producer rather than on the mover whose
        # quality gate held it, because `raise_workflow_event` resolves a workflow instance through the
        # CALLING app's app-id: route and instance must share a process, and a mover is bus-only — no
        # row here, no Ingress path. Root-mounted like /produce and /train (the producer does not use
        # `make_service_app`'s prefix), so the rewrite is a literal, not `prefix`-interpolated.
        ("/api/promotions", "/promotions", *medallion),
        # The cascade's operator surface. Routed to the PRODUCER, which authorizes and forwards to the
        # mover that hosts the instance — a mover has no row of its own because it is bus-only, and
        # `terminate_workflow` must run under the mover's app-id, so neither end can do both halves.
        ("/api/movers", "/movers", *medallion),
        (f"{prefix}/ray", f"{prefix}/ray", *compute),
        (f"{prefix}/projects", f"{prefix}/projects", *controlplane),
        # PREFIX-INTERPOLATED, not the literal "/api/flows", and that is the ingest row's lesson
        # applied rather than restated: the flows service is composed by `make_service_app`, which
        # mounts every router under `settings.api_prefix` — the SAME env var `_routes` reads at the top.
        # So public and upstream track the prefix together and cannot drift apart, which is exactly what
        # a hardcoded pair could do the moment RASK_API_PREFIX is not "/api". Under the chart's and
        # dev-micro.sh's prefix this row IS "/api/flows" → "/api/flows".
        # tests/test_routing.py pins the rewrite against the flows app's own openapi, not against a
        # reading of it.
        (f"{prefix}/flows", f"{prefix}/flows", *flows),
        # PREFIX-INTERPOLATED for the flows row's reason, not by imitation: `notifications` is composed
        # by `make_service_app` too, so its routers mount under `settings.api_prefix` — the same env var
        # read at the top of this function. A hardcoded "/api/notifications" pair would be correct only
        # while RASK_API_PREFIX happens to be "/api".
        (f"{prefix}/notifications", f"{prefix}/notifications", *notifications),
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


def _normalize_raw_path(raw: bytes) -> bytes:
    """`_normalize_path`'s twin for the ENCODED path: classify by decoded value, keep original bytes.

    The two must agree about what a segment IS while disagreeing about how it is spelled. A byte-wise
    walker would read `%2e%2e` as an ordinary name and let a traversal through; a decoding walker
    that also RETURNED the decoded bytes would turn `%2F` into a separator and address a different
    object. So each segment is decoded only to be classified, and the original bytes are what survive.
    """
    kept: list[bytes] = []
    for raw_seg in raw.split(b"/"):
        seg = unquote(raw_seg.decode("ascii"))
        if seg in ("", "."):
            continue
        if seg == "..":
            if kept:
                kept.pop()
            continue
        kept.append(raw_seg)
    normalized = b"/" + b"/".join(kept)
    if raw.endswith(b"/") and not normalized.endswith(b"/"):
        normalized += b"/"
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


# DOCS ARE OPT-IN AT THE FRONT DOOR (`RASK_DOCS`), and this is the one that mattered most: the chart
# publishes `/api` at the Ingress, so everything the gateway serves under the prefix is reachable
# unauthenticated from the internet.
_docs_enabled = os.environ.get("RASK_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="gateway",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

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
    path = _normalize_path(request.scope["path"]).lower()
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

    # TWO VIEWS OF THE PATH, and the whole correctness of this hop is in keeping them apart.
    #
    # `request.url.path` is neither of them and must not be used here. Starlette builds `URL` from a
    # reassembled STRING and re-splits it with urlsplit, so a path segment that decodes to `?` or `#`
    # silently truncates it: for `/api/catalog/v1/table/x%23y/describe`, `scope["path"]` is
    # `…/table/x#y/describe` but `request.url.path` is `…/table/x`. Every decision below — the
    # blocklist, the route pick, the upstream rewrite — was being taken against a path that is not
    # the one the upstream would execute.
    #
    # DECIDE on the decoded path (`scope["path"]`): normalisation must see `%2E%2E` as a traversal,
    # which is what stops the sidecar-only lineage routes being reached by spelling the dots in hex.
    # FORWARD the raw path (`scope["raw_path"]`): `%2F` is a literal character inside one segment,
    # never a separator, and re-encoding cannot restore a distinction already lost to decoding. That
    # is also what carries the Lance Namespace multipart delimiter, the unit separator 0x1F — every
    # nested namespace answered an unhandled 500 (`httpx.InvalidURL`) while flat ids answered 200.
    scope_path: str = request.scope["path"]
    # raw_path is optional in ASGI; uvicorn and the TestClient both set it, query-stripped and
    # root_path-prefixed exactly like scope["path"], so the two stay in lockstep. The fallback keeps
    # a server that omits it working rather than crashing.
    raw_path: bytes = request.scope.get("raw_path") or quote(scope_path).encode("ascii")
    # A URL path is ASCII by construction (RFC 3986); anything else had to arrive as raw bytes on the
    # wire rather than percent-encoded. Refused HERE rather than at the URL build, because the
    # normaliser below decodes each segment and would raise UnicodeDecodeError first — an unhandled
    # 500 for client-controlled input, which is the class of answer this whole function is removing.
    if not raw_path.isascii():
        raise HTTPException(status_code=400, detail="request path contains non-ASCII bytes")

    # Serve a unified API page aggregating every backend's schema, instead of
    # proxying /docs + /openapi.json to core-api only.
    #
    # BEHIND `RASK_DOCS`, because these two branches are not like the others: they are the only routes
    # the gateway ANSWERS itself under the prefix, they take no dependency and check no token, and
    # `chart/templates/ingress.yaml` publishes `/api` — so an anonymous request from the internet got
    # the merged route table, parameter names and request/response schemas of every backend the
    # gateway fronts. It is also an amplification lever: one unauthenticated GET costs the gateway a
    # sequential fan-out to every distinct upstream at a 10 s timeout each.
    #
    # 404 rather than 403 when off, so the answer is indistinguishable from a gateway that never had
    # a docs route — the same authz-before-existence rule the promotion read follows.
    if scope_path in {f"{prefix}/openapi.json", f"{prefix}/docs"}:
        if not _docs_enabled:
            raise HTTPException(status_code=404, detail="Not Found")
        if scope_path == f"{prefix}/openapi.json":
            return JSONResponse(await _merged_openapi(client, prefix, _distinct_targets(request.app.state.routes)))
        return get_swagger_ui_html(openapi_url=f"{prefix}/openapi.json", title="rask API (gateway)")

    norm_path = _normalize_path(scope_path)
    picked = _pick_route(norm_path, request.app.state.routes)
    if picked is None:
        raise HTTPException(status_code=404, detail=f"no upstream for {scope_path}")
    route_prefix, upstream_prefix, app_id, fallback = picked
    base = _target_base(app_id, fallback)

    raw_norm = _normalize_raw_path(raw_path)
    # The one input where the two views genuinely disagree: a dot-segment hidden behind an encoded
    # slash (`a%2F..%2Fb`) is ONE segment byte-wise and THREE after decoding. Forwarding the raw
    # bytes would execute a path the blocklist never evaluated; collapsing them would address an
    # object the client never named. Refusing is strictly safer than picking a side, and no
    # legitimate client sends it. Every other case agrees and passes through.
    if unquote(raw_norm.decode("ascii")) != norm_path or not raw_norm.decode("ascii").startswith(route_prefix):
        raise HTTPException(status_code=400, detail="path encoding is ambiguous")

    # The upstream prefix replaces the public one: /api/catalog/v1/x → /v1/x,
    # /api/explorer/search?q → /api/search?q. rask rows rewrite to themselves.
    # Assembled as BYTES through copy_with(raw_path=...) rather than an f-string, because an f-string
    # hands httpx a URL to re-parse and `:`/`?`/`#` in the path would be read as delimiters again —
    # reintroducing the very truncation this function exists to avoid. The base may carry its own
    # path (the Dapr invoke form, `/v1.0/invoke/<app>/method`), so it is prepended, not replaced.
    base_url = httpx.URL(base)
    upstream_raw = base_url.raw_path.rstrip(b"/") + upstream_prefix.encode("ascii") + raw_norm[len(route_prefix) :]
    query: bytes = request.scope.get("query_string") or b""
    try:
        url = base_url.copy_with(raw_path=upstream_raw + (b"?" + query if query else b""))
    except (httpx.InvalidURL, UnicodeDecodeError) as exc:
        # What remains unrepresentable is client-controlled: a non-ASCII byte or a literal control
        # character sent raw on the wire rather than percent-encoded. That is a bad request, not a
        # broken backend — answering 502 here would tell an operator a healthy service is down.
        raise HTTPException(status_code=400, detail=f"unrepresentable request path: {exc}") from exc
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
