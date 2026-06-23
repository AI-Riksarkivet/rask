"""gateway — the single API origin for the SPA.

Path-routes `/api/*` to the per-domain backends and streams responses back. This
is the frontend proxy target (`:8888`), so the SPA needs no changes. Does not
import `viewer` — it only forwards HTTP.

Routing is longest-prefix-first; upstreams are env-overridable with localhost
defaults that match `Procfile.micro`.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


log = logging.getLogger("gateway")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailers", b"transfer-encoding", b"upgrade", b"host"}
)


def _routes() -> list[tuple[str, str, str]]:
    # (path-prefix, dapr app-id, httpx fallback URL). Mirror RASK_API_PREFIX so
    # routing lines up with where endpoints mount. load_dotenv() so the gateway
    # sees the same .env config the backends do.
    load_dotenv()
    prefix = os.environ.get("RASK_API_PREFIX", "/api/v1").rstrip("/")
    core = ("core-api", os.environ.get("RASK_CORE_API_URL", "http://127.0.0.1:8801"))
    search = ("search-api", os.environ.get("RASK_SEARCH_API_URL", "http://127.0.0.1:8802"))
    volumes = ("volumes-api", os.environ.get("RASK_VOLUMES_API_URL", "http://127.0.0.1:8803"))
    ray = ("ray-api", os.environ.get("RASK_RAY_API_URL", "http://127.0.0.1:8804"))
    orch = ("orchestrator", os.environ.get("RASK_ORCH_API_URL", "http://127.0.0.1:8810"))
    # longest / most-specific prefixes first; the prefix itself is the catch-all
    return [
        (f"{prefix}/search", *search),
        (f"{prefix}/volumes", *volumes),
        (f"{prefix}/ray", *ray),
        (f"{prefix}/orchestrator", *orch),
        ("/api/serve", *ray),
        (prefix, *core),
        ("/api", *core),
    ]


def _pick_route(path: str, routes: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    for prefix, app_id, fallback in routes:
        if path == prefix or path.startswith(prefix + "/"):
            return app_id, fallback
    return None


def _dapr_enabled() -> bool:
    return os.environ.get("RASK_DAPR_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _target_base(app_id: str, fallback_url: str) -> str:
    """Invoke base for an app: the local Dapr sidecar when enabled, else the
    direct httpx upstream URL. Append the request path to this to get the URL."""
    if _dapr_enabled():
        port = os.environ.get("DAPR_HTTP_PORT", "3500")
        return f"http://127.0.0.1:{port}/v1.0/invoke/{app_id}/method"
    return fallback_url


def _distinct_targets(routes: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Unique (app_id, fallback_url) pairs, first-seen order (for openapi merge)."""
    seen: dict[str, str] = {}
    for _, app_id, fallback in routes:
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
    for prefix, app_id, fallback in app.state.routes:
        log.info(f"route {prefix} -> {app_id} ({_target_base(app_id, fallback)})")
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="gateway", version="0.1.0", lifespan=lifespan)


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

    picked = _pick_route(request.url.path, request.app.state.routes)
    if picked is None:
        raise HTTPException(status_code=404, detail=f"no upstream for {request.url.path}")
    app_id, fallback = picked
    base = _target_base(app_id, fallback)

    url = httpx.URL(f"{base}{request.url.path}").copy_with(query=request.url.query.encode("utf-8") or None)
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in _HOP_BY_HOP]
    upstream_req = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        # Upstream unreachable (not started yet, crashed, wrong port) — surface a
        # clean 502 rather than a 500 traceback.
        raise HTTPException(status_code=502, detail=f"upstream {base} unreachable: {exc}") from exc

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower().encode() not in _HOP_BY_HOP},
        background=BackgroundTask(upstream_resp.aclose),
    )
