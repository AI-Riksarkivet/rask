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

#: The spec's READ actions, as trailing path segments.
#:
#: THE METHOD IS NOT ENOUGH HERE, and that is a property of the Lance Namespace grammar rather than an
#: oversight in the middleware. The route shape is ``POST /v1/<object>/{id}/<action>`` — the spec puts
#: the operation in the PATH, so `describe`, `count_rows`, `query` and every other read arrive as POST.
#: Classifying by verb alone therefore refused the entire read surface, which is the outage a read-only
#: window exists to avoid rather than cause.
#:
#: A SUFFIX ALLOWLIST, so classification is fail-closed: an action nobody has listed is treated as a
#: write. A spec operation added tomorrow is refused during a window until someone judges it, which is
#: the safe direction — the opposite default would make every future write a silent hole discovered
#: only when it corrupted something mid-maintenance.
_READ_ACTIONS: frozenset[str] = frozenset(
    {
        "describe",
        "exists",
        "list",
        "count_rows",
        "query",
        "stats",
        "explain_plan",
        "analyze_plan",
        "my-permissions",
        "check",
        "graph",
        "tasks",
        "preview",
    }
)


def is_mutating(method: str, path: str = "") -> bool:
    """True when this request WRITES — by action where the spec names one, else by method.

    ``path`` is optional so existing callers keep working; without it the answer degrades to the
    method rule, which is correct for the non-spec surface and merely insufficient for ``/v1``.
    """
    if method.upper() in _SAFE_METHODS:
        return False
    action = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
    return action not in _READ_ACTIONS


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
    if read_only and request.url.path.startswith("/v1") and is_mutating(request.method, request.url.path):
        return maintenance_response()
    return await call_next(request)
