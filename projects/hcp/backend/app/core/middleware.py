"""HTTP middleware — request ID, OTel correlation, and access logging."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import Request
from opentelemetry import metrics, trace
from starlette.middleware.base import BaseHTTPMiddleware

access_logger = logging.getLogger("access")

# Probe paths are excluded from access logging to avoid noise.
_HEALTH_PATHS = frozenset({"/liveness", "/readiness", "/health"})

_meter = metrics.get_meter(__name__)
_http_request_duration = _meter.create_histogram(
    "http.server.request.duration",
    description="HTTP request duration in milliseconds",
    unit="ms",
)
_http_request_count = _meter.create_counter(
    "http.server.request.count",
    description="Total HTTP requests",
    unit="1",
)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Request ID, OTel trace correlation, and per-request access logging."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("request.id", request_id)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        response.headers["X-Request-ID"] = request_id

        metric_attrs = {
            "http.method": request.method,
            "http.status_code": response.status_code,
            "http.route": request.url.path,
        }
        _http_request_duration.record(duration_ms, metric_attrs)
        _http_request_count.add(1, metric_attrs)

        if request.url.path not in _HEALTH_PATHS:
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else None
            span_id = format(ctx.span_id, "016x") if ctx.span_id else None

            # Best-effort extraction — no signature validation, just for logging context
            user = None
            tenant = None
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                try:
                    import jwt

                    payload = jwt.decode(auth[7:], options={"verify_signature": False})
                    user = payload.get("sub")
                    tenant = payload.get("tenant")
                except Exception:
                    pass

            access_logger.info(
                "%s %s %s %sms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.query_params) or None,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "user": user,
                    "tenant": tenant,
                    "client_ip": request.client.host if request.client else None,
                    "trace_id": trace_id,
                    "span_id": span_id,
                },
            )

        return response
