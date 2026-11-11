"""Read-only maintenance mode.

Rejects mutating requests on both mounts — the spec's ``/v1`` and rask's ``/management/v1`` — with
``503 + Retry-After`` while
``LANCE_MAINTENANCE_READ_ONLY`` is set — for zero-downtime model/schema
migration windows (flip on, migrate, flip off). Health and read endpoints stay available, and the catalog's
cron ticks, delivered at the pod root, are answered and do nothing. Default OFF, so this is a no-op unless
explicitly enabled. The chart renders the flag from its one `readOnlyWindow` value, which also holds maintenance's
worker and lineage's relay, so one value in git puts the whole lakehouse in the window.

NOT the table maintenance surface: version cleanup is ``services/maintenance``'s expiry pass
(``maintenance.services.expiry``), and compaction and index builds are bring-your-own jobs whose commits are judged
at ``version/create`` (ADR 0174). This module only gates writes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from lance_namespace import ErrorCode
from starlette.routing import Match

from catalog.api.cron import ticks_of
from catalog.core import vending
from catalog.core.config import Settings
from lakehouse.ns_errors import problem_body


PROBLEM_JSON = "application/problem+json"
RETRY_AFTER_SECONDS = 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

log = logging.getLogger(__name__)

#: Every mount that serves the catalog's doors. The management mount carries the most destructive ones
#: (erasure, policy set, grants, the registries), so a window that gated only `/v1` let each of them run
#: through it (LH-446).
GATED_MOUNTS: tuple[str, ...] = ("/v1/", "/management/v1/")

#: The spec's READ actions, as the last segment of the ROUTE a POST matches.
#:
#: THE METHOD IS NOT ENOUGH HERE, and that is a property of the Lance Namespace grammar rather than an
#: oversight in the middleware. The route shape is ``POST /v1/<object>/{id}/<action>`` — the spec puts
#: the operation in the PATH, so `describe`, `count_rows`, `query` and every other read arrive as POST.
#: Classifying by verb alone would refuse the entire read surface, which is the outage a read-only
#: window exists to avoid rather than cause.
#:
#: THE ROUTE, NEVER THE PATH TEXT: a path's last segment can be a name its caller chose, so a project named `list`
#: would read as a read action. The route template's last segment is a literal only where the route names an action.
#:
#: AN ALLOWLIST, so classification is fail-closed: an action nobody has listed is treated as a
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
        # rask's management reads, served as POST in the same grammar: the change feed and the estate FGA
        # workbench's derivations.
        "changes",
        "list-objects",
        "list-users",
        "expand",
        "simulate",
    }
)

#: The vend door's action. A read-tier vend is how a direct pylance reader reads, so it is served
#: through a window; a write or maintain tier hands out the means to write storage straight past the
#: catalog, so it is refused like any other write. The tier rides the query (`credentials.py`, default
#: `read`), which the path alone cannot show.
_VEND_ACTION = "credentials"


def is_mutating(request: Request) -> bool:
    """Whether ``request`` writes, classified by the route it matches and never by its path text.

    GET, HEAD and OPTIONS read. DELETE, PUT and PATCH write whatever they name. A POST reads only when the route it
    matches names a read action (the spec's grammar puts the action in the path); the vend door reads at the read tier
    alone, which rides its query. A POST matching no route writes: classification fails closed.

    Args:
        request: The request, its path as routing reads it (``scope["path"]``, [[LH-526]]).

    Returns:
        True when the request writes, or when nothing shows that it only reads.
    """
    method = request.method.upper()
    if method in _SAFE_METHODS:
        return False
    if method != "POST":
        return True
    template = _matched_route(request)
    if template is None:
        return True
    action = template.rsplit("/", 1)[-1]
    if action == _VEND_ACTION:
        return vending.requested_tier(request.query_params) != "read"
    return action not in _READ_ACTIONS


def _matched_route(request: Request) -> str | None:
    """The template of the route ``request`` matches in full, as the router would pick it, or None when none does.

    Walked as the router walks it (the first full match, in order), over every route an included router contributes:
    this FastAPI keeps an included router as one opaque route whose own match reports nothing of the route inside it.
    """
    for route in iter_route_contexts(request.app.routes):
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return route.path_format
    return None


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


def _held_tick_response() -> JSONResponse:
    """The answer to a cron tick through a window: a 2xx, so the sidecar counts no failed delivery, and a tick that did
    nothing, as a tick that finds another one running says."""
    return JSONResponse(status_code=200, content={"skipped": True})


async def maintenance_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Short-circuit mutating requests on every catalog mount with 503, and hold every cron tick, when read-only is on."""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None or not settings.maintenance_read_only:
        return await call_next(request)
    # The path routing matches, never `request.url.path`: that re-parses the decoded path as a URL, so a `?` in a name
    # would end the path there ([[LH-526]]).
    path: str = request.scope["path"]
    if request.method == "POST" and path in ticks_of(request.app):
        log.info("cron_tick_held_for_read_only_window", extra={"binding": path.lstrip("/")})
        return _held_tick_response()
    if path.startswith(GATED_MOUNTS) and is_mutating(request):
        return maintenance_response()
    return await call_next(request)
