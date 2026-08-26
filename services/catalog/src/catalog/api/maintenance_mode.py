"""Read-only maintenance mode.

Rejects mutating ``/v1`` requests with ``503 + Retry-After`` while
``LANCE_MAINTENANCE_READ_ONLY`` is set — for zero-downtime model/schema
migration windows (flip on, migrate, flip off). Mutating = anything other than
GET / HEAD / OPTIONS; health and read endpoints stay available. Default OFF, so
this is a no-op unless explicitly enabled.

NOT the table-maintenance surface: compaction, version cleanup and index optimize live in
``services/maintenance`` (``maintenance.services.optimize``). This module only gates writes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode

from service_kit.lakehouse.ns_errors import problem_body


PROBLEM_JSON = "application/problem+json"
RETRY_AFTER_SECONDS = 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_mutating(method: str) -> bool:
    """True for HTTP methods that write (anything but GET/HEAD/OPTIONS)."""
    return method.upper() not in _SAFE_METHODS


def maintenance_response() -> JSONResponse:
    """Build the standardized 503 + ``Retry-After`` problem+json response."""
    return JSONResponse(
        status_code=503,
        # SHARED builder: the spec's `code` is required on the generated client's ErrorResponse, and
        # this body is emitted on `/v1` routes those clients call. The response is still built here
        # rather than raised, because `Retry-After` carries the maintenance WINDOW (60s, not the
        # draining 5s) and the installed handler emits no headers at all.
        content=problem_body(
            ErrorCode.SERVICE_UNAVAILABLE,
            status=503,
            title="MaintenanceMode",
            slug="maintenance",
            detail="Catalog is in read-only maintenance mode; retry after the window.",
        ),
        media_type=PROBLEM_JSON,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


async def maintenance_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Short-circuit mutating ``/v1`` requests with 503 when read-only is on."""
    settings = getattr(request.app.state, "settings", None)
    read_only = bool(getattr(settings, "maintenance_read_only", False))
    if read_only and is_mutating(request.method) and request.url.path.startswith("/v1"):
        return maintenance_response()
    return await call_next(request)
