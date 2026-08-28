"""HTTP middleware registration.

Only CORS is registered. Deliberately NO ``BaseHTTPMiddleware``-based
RequestID/Timing middleware: ``BaseHTTPMiddleware`` fully buffers the response
body, which would break the ``/api/explorer`` ``StreamingResponse`` Range streaming
(206 partial-content video seeking). If per-request IDs/timing are ever needed,
use a pure ASGI middleware that passes through streaming bodies, or wire it via
OpenTelemetry's ASGI instrumentation — not ``BaseHTTPMiddleware``.

``expose_headers`` is load-bearing: the browser needs Content-Range /
Content-Length / Accept-Ranges visible to seek video.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.media.config import Settings
from service_kit.middleware import RequestIDMiddleware


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Register CORS and the request-body ceiling. Called once from ``create_app``."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # The annotator serves PUT/PATCH/DELETE (members, drafts, ontology, project events); with the
        # write verbs absent, a cross-origin browser preflight for them is answered without them in
        # Access-Control-Allow-Methods and the real request is blocked. These are the methods the
        # media apps actually route.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
    )
    # THE BODY CEILING, and these are the apps that most needed one: viewer, search and annotator are
    # where multipart uploads arrive. A file part is spooled whole to disk BEFORE the handler runs, so
    # a handler-side `read(cap + 1)` bounds that handler's memory and nothing about the landing zone.
    # Pure-ASGI, so this refuses as bytes arrive — before starlette spools anything.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    # THE EXEMPTION THIS MODULE RECORDED IS OVER. Its docstring refused a `BaseHTTPMiddleware`
    # RequestID/Timing pair because that class fully buffers the response body and would break the
    # `/api/explorer` Range streaming 206 seeking depends on — and it named the remedy: "use a pure ASGI
    # middleware that passes through streaming bodies". `RequestIDMiddleware` is exactly that now, so
    # these three apps get the same one id every other service has, and the streaming they traded it for
    # is untouched: only the response START message is rewritten, never a body chunk.
    app.add_middleware(RequestIDMiddleware)
