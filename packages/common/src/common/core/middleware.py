"""HTTP middleware registration.

Only CORS is registered. Deliberately NO ``BaseHTTPMiddleware``-based
RequestID/Timing middleware: ``BaseHTTPMiddleware`` fully buffers the response
body, which would break the ``/api/media`` ``StreamingResponse`` Range streaming
(206 partial-content video seeking). If per-request IDs/timing are ever needed,
use a pure ASGI middleware that passes through streaming bodies, or wire it via
OpenTelemetry's ASGI instrumentation — not ``BaseHTTPMiddleware``.

``expose_headers`` is load-bearing: the browser needs Content-Range /
Content-Length / Accept-Ranges visible to seek video.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.core.config import Settings


def register_middleware(app: FastAPI, settings: Settings) -> None:
    """Register CORS. Called once from ``create_app`` after ``register_handlers``."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
    )
