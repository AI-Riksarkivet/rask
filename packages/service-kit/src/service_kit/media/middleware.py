"""The MEDIA plane's HTTP middleware stack: CORS, the request-body ceiling, one request id.

NO LAYER ON THIS PLANE MAY BE ``BaseHTTPMiddleware``-BASED — a constraint on anything added later, not
a description of what happens to be here. ``BaseHTTPMiddleware`` fully buffers the response body,
which breaks the ``/api/explorer`` ``StreamingResponse`` Range streaming that 206 partial-content
video seeking depends on. Each layer below is pure ASGI: it rewrites only the response START message,
or refuses before the body is read, so a streaming response passes through chunk for chunk. A timing
or auth layer wanted here is written the same way.

``expose_headers`` is load-bearing: the browser needs Content-Range /
Content-Length / Accept-Ranges visible to seek video.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.media.config import Settings
from service_kit.middleware import RequestIDMiddleware


def register_media_middleware(app: FastAPI, settings: Settings) -> None:
    """Register CORS and the request-body ceiling for a MEDIA app. Called once from ``build_media_app``.

    NAMED FOR ITS PLANE (docs/DECISIONS.md "The Python estate audit" DUP-20). This was `register_middleware`, the same name and
    the same signature as `service_kit.middleware.register_middleware` — a DIFFERENT stack (the fleet
    one adds Timing and sends `allow_credentials`; this one exposes the Range headers a browser needs
    to seek video and deliberately runs no Timing layer). Both were imported bare, so a call site said
    nothing about which of the two it had registered."""
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
    # ONE ID PER REQUEST, on this plane too: `RequestIDMiddleware` is pure ASGI and rewrites only the
    # response START message, never a body chunk, so the media trio gets the same correlation id every
    # other service has and the Range streaming above is untouched.
    app.add_middleware(RequestIDMiddleware)
