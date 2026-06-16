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

from service_kit.config import Settings


_REQUEST_ID_HEADER = "X-Request-ID"
_RESPONSE_TIME_HEADER = "X-Response-Time"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Echo or generate `X-Request-ID` per request; attach to `request.state`.

    Inbound `X-Request-ID` is preserved so a reverse proxy / client can
    correlate logs across hops. Without it, we mint a UUID hex.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response


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
