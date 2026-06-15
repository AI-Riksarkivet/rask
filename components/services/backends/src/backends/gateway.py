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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


log = logging.getLogger("backends.gateway")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailers", b"transfer-encoding", b"upgrade", b"host"}
)


def _routes() -> list[tuple[str, str]]:
    # Mirror the backends' API prefix (RASK_API_PREFIX, e.g. /api/v1 or /api) so
    # routing lines up with where the endpoints actually mount. load_dotenv() so
    # the gateway sees the same .env config the backends do.
    load_dotenv()
    prefix = os.environ.get("RASK_API_PREFIX", "/api/v1").rstrip("/")
    core = os.environ.get("RASK_CORE_API_URL", "http://127.0.0.1:8801")
    search = os.environ.get("RASK_SEARCH_API_URL", "http://127.0.0.1:8802")
    volumes = os.environ.get("RASK_VOLUMES_API_URL", "http://127.0.0.1:8803")
    ray = os.environ.get("RASK_RAY_API_URL", "http://127.0.0.1:8804")
    orch = os.environ.get("RASK_ORCH_API_URL", "http://127.0.0.1:8810")
    # longest / most-specific prefixes first; the prefix itself is the catch-all
    return [
        (f"{prefix}/search", search),
        (f"{prefix}/volumes", volumes),
        (f"{prefix}/ray", ray),
        (f"{prefix}/orchestrator", orch),
        ("/api/serve", ray),
        (prefix, core),
        ("/api", core),
    ]


def _pick_upstream(path: str, routes: list[tuple[str, str]]) -> str | None:
    for prefix, upstream in routes:
        if path == prefix or path.startswith(prefix + "/"):
            return upstream
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
    app.state.routes = _routes()
    for prefix, upstream in app.state.routes:
        log.info(f"route {prefix} -> {upstream}")
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="gateway", version="0.1.0", lifespan=lifespan)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request) -> StreamingResponse:
    upstream = _pick_upstream(request.url.path, request.app.state.routes)
    if upstream is None:
        raise HTTPException(status_code=404, detail=f"no upstream for {request.url.path}")

    client: httpx.AsyncClient = request.app.state.http
    url = httpx.URL(f"{upstream}{request.url.path}").copy_with(query=request.url.query.encode("utf-8") or None)
    headers = [(k, v) for k, v in request.headers.raw if k.lower() not in _HOP_BY_HOP]
    upstream_req = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        # Upstream unreachable (not started yet, crashed, wrong port) — surface a
        # clean 502 rather than a 500 traceback.
        raise HTTPException(status_code=502, detail=f"upstream {upstream} unreachable: {exc}") from exc

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower().encode() not in _HOP_BY_HOP},
        background=BackgroundTask(upstream_resp.aclose),
    )
