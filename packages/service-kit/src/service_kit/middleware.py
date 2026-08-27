"""HTTP middleware — cross-cutting request concerns.

Order matters. FastAPI runs middleware in reverse of registration order on
the way in, then re-reverses on the way out. The last `add_middleware(...)`
call wraps everything else and runs first.

Registered order (per `fastapi/references/production-patterns.md` § Middleware):

  1. CORS         — only added when `settings.cors_origins` is non-empty
  2. RequestID    — sets `request.state.request_id`, echoes `X-Request-ID`
  3. Timing       — sets `X-Response-Time` header (perf debug)

Logging middleware deferred until structured logging / OTel lands — the
`otel` skill auto-instruments stdlib `logging` records once we wire the
SDK; emitting a custom log line per request is duplicate work until then.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.config import Settings
from service_kit.context import request_id_ctx


_REQUEST_ID_HEADER = "X-Request-ID"
_RESPONSE_TIME_HEADER = "X-Response-Time"


class RequestIDMiddleware:
    """Echo or generate `X-Request-ID` per request; publish it to `request.state` and the context var.

    Inbound `X-Request-ID` is preserved so a reverse proxy / client can correlate logs across hops.
    Without it, we mint a UUID hex.

    PURE ASGI, NOT `BaseHTTPMiddleware`, and that is what makes it safe everywhere. `BaseHTTPMiddleware`
    fully buffers the response body — `service_kit/media/middleware.py` records exactly this, and it is
    why viewer/search/annotator deliberately ran NO request-id middleware: it would have broken the
    `/api/explorer` Range streaming that 206 video seeking depends on, and the catalog's Arrow-IPC data
    plane has the same shape. Its own docstring named the remedy: "use a pure ASGI middleware that
    passes through streaming bodies". This is that, so the exemption is no longer needed and every
    service can carry one id.

    It touches only the response START message to add the header; body chunks pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get(_REQUEST_ID_HEADER.lower()) or uuid.uuid4().hex
        # `scope["state"]` is what `request.state` reads, so existing `request.state.request_id`
        # consumers keep working without knowing this middleware changed shape.
        scope.setdefault("state", {})["request_id"] = request_id

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                raw.append((_REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        # PUBLISHED TO THE CONTEXT, so code that does not take `request` — a log record, a repository
        # method, a background helper — can read it without plumbing. Before this the id was minted,
        # echoed and read by nothing: a caller could quote it and an operator had nothing to grep.
        token = request_id_ctx.set(request_id)
        try:
            await self.app(scope, receive, _send)
        finally:
            # THE RESET IS THE LOAD-BEARING HALF. Without it the value survives the response on a
            # reused worker and labels the NEXT request with the previous caller's id — worse than no
            # id at all, because it correlates the wrong things while looking correct.
            request_id_ctx.reset(token)


class TimingMiddleware(BaseHTTPMiddleware):
    """Set `X-Response-Time` header with handler wall time."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        response.headers[_RESPONSE_TIME_HEADER] = f"{time.perf_counter() - start:.3f}s"
        return response


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Register all HTTP middleware on the app.

    Called once from `create_app()` after `register_handlers()`. Order is
    significant — see module docstring.
    """
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["*"],
            expose_headers=[_REQUEST_ID_HEADER, _RESPONSE_TIME_HEADER],
        )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(TimingMiddleware)
    # THE BODY CEILING, for every service rather than the one it was written for. Pure-ASGI, so the
    # cap applies as bytes ARRIVE — before the body is buffered and independent of how the endpoint
    # reads it. Added last so it sits OUTERMOST: an over-cap request is refused before RequestID or
    # Timing do any work on it.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
