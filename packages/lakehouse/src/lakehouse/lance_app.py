"""THE lakehouse's app factory — one assembly for catalog, lineage and maintenance.

Routers mount at the root under each service's own paths (``/v1/...``, ``/events``, a cron binding's ``/<name>``),
config is each service's own ``*Settings``, and every layer a request passes through is installed here, in one order.

docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12 counted eight module-level ``app = FastAPI(...)`` entrypoints
hand-assembling
the same boot and asked for a ``make_lance_service_app``. This serves catalog, lineage and maintenance, and
what it owns is the part that is identical in all of them:

1. ``setup_logging()`` BEFORE the app exists. Every module in these services uses
   ``getLogger(__name__)``, and without this they propagate to a root logger with no handlers and are
   DISCARDED — which is not hypothetical, it hid a two-day lineage feed outage.
2. The ``FastAPI`` construction with docs OPT-IN. ``/docs``, ``/redoc`` and ``/openapi.json`` are all
   gated on the service's own ``docs_enabled``; a service that sets none of the three serves all
   three, which is how the schemas shipped openly once. Each page names its schema by a URL relative to itself, and
   the schema its server the same way (``_SERVED_BESIDE_THE_SCHEMA``): a service is published under a prefix its route
   rewrites or the service strips, which it is never told, so only a relative URL is right behind every route.
3. ``register_handlers`` THEN ``install_problem_handlers``. ``lakehouse.exceptions.DomainError``
   subclasses ``HTTPException``, so without the first, starlette's built-in handler renders it —
   status and headers intact, ``{"detail": ...}`` body — which is how the draining 503 came to declare
   problem+json over a body that was not one. ``install_problem_handlers`` owns request validation and the
   catch-all, so the two sets map disjoint classes.
4. The layers every request passes, outermost first (ADR 0203):

   * ``RequestIDMiddleware``. One id per request, minted or echoed, published to the context var
     ``setup_logging``'s filter reads, so a caller can quote an id from a failed request and an operator
     can grep for it. OUTERMOST, so every refusal a layer below answers carries it. PURE ASGI, so it passes
     streaming bodies through untouched, which is what makes it safe on a plane that serves Arrow IPC.
   * ``PanicCatcherMiddleware``: a native library's panic answers 500 with the problem body and the process
     serves on.
   * ``RequestDeadlineMiddleware``: a request with no answer begun within ``request_deadline_seconds`` is
     answered 503 while its door runs on to its end. Outside the caller's ``outer_middleware``, so a door
     running on past its deadline still holds the write seat the catalog's shed counted it in.
   * ``outer_middleware``, the caller's own, in its order (the catalog's path reading and write shed).
   * ``BodySizeLimitMiddleware`` at ``max_body_bytes``, a body over it refused 413 before it is buffered.
   * ``inner_middleware``, the caller's ``http`` functions that must run INSIDE every other layer: the
     catalog's read-only gate and signing gate answer through the request-id layer this way.
5. ``/livez`` + ``/readyz`` at the ROOT — a kubelet does not know a service's prefix.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Final

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from starlette.middleware import Middleware

from lakehouse.app import setup_logging
from lakehouse.body_limit import BodySizeLimitMiddleware
from lakehouse.deadline import RequestDeadlineMiddleware
from lakehouse.exceptions import register_handlers
from lakehouse.governed.audit import configure_audit
from lakehouse.middleware import RequestIDMiddleware
from lakehouse.ns_errors import install_problem_handlers
from lakehouse.panic_catcher import PanicCatcherMiddleware
from lakehouse.probes import LiveCheck, ReadyCheck, make_probes_router


#: Lance's switch for the migration a read commits. A dataset whose fragment metadata is out of date, or whose index
#: segments overlap, is migrated by a commit from whichever read noticed, ``index_stats`` among them, unless it is
#: ``false``. Lance reads it at the call, not at import (measured on pylance 13.0.0).
_AUTO_MIGRATION: Final = "LANCE_AUTO_MIGRATION"


def _forbid_migrations_on_read() -> None:
    """Keep every Lance read in this process from committing to the table it reads.

    No lakehouse plane writes a table but through a door that judged the write. Measured on pylance 13.0.0: an
    ``index_stats`` read of two index segments covering one fragment committed 472 migrations and then crashed the
    process; with the switch off the read raises and the table is unchanged. Set whatever the environment names, as
    a write a read makes is never one this process may make.
    """
    os.environ[_AUTO_MIGRATION] = "false"


#: The schema's one server, relative to wherever the schema itself was fetched from (OpenAPI resolves a relative server
#: URL against the document's own), so a client reading it under a public prefix calls the doors under that prefix.
_SERVED_BESIDE_THE_SCHEMA: Final = "."

#: The schema as each docs page names it: beside the page, whatever prefix the page was reached under.
_SCHEMA_BESIDE_THE_PAGE: Final = "openapi.json"


def _mount_docs(app: FastAPI) -> None:
    """Serve ``/docs`` and ``/redoc``, each fetching the schema beside itself."""

    async def swagger() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=_SCHEMA_BESIDE_THE_PAGE, title=f"{app.title} - Swagger UI")

    async def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url=_SCHEMA_BESIDE_THE_PAGE, title=f"{app.title} - ReDoc")

    app.add_api_route("/docs", swagger, methods=["GET"], include_in_schema=False)
    app.add_api_route("/redoc", redoc, methods=["GET"], include_in_schema=False)


def build_lance_service_app(
    *,
    title: str,
    version: str = "0.1.0",
    docs_enabled: bool,
    audit_enabled: bool,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    log: logging.Logger,
    ready_check: ReadyCheck,
    max_body_bytes: int,
    request_deadline_seconds: float,
    live_check: LiveCheck | None = None,
    routers: Sequence[APIRouter] = (),
    inner_middleware: Sequence[Callable[[Any, Any], Any]] = (),
    outer_middleware: Sequence[Middleware] = (),
) -> FastAPI:
    """Assemble one lance-plane service's FastAPI app. See the module docstring for what and why.

    ``log`` is the SERVICE's logger, not this module's: the problem handlers log an unhandled error
    with a traceback, and that record must carry the name of the service it happened in.

    ``ready_check`` is the service's own readiness answer, run once the lifecycle flags say started and not draining.
    REQUIRED, as ``docs_enabled`` is: every lakehouse service has a dependency or a fact `/readyz` reports (the
    catalog's namespace and signing key, lineage's feed, maintenance's signing key), so a new one answers it too.

    ``live_check`` is for a process whose probes are not served by the loop its work runs on (a worker serving them
    from a thread): what `/livez` then answers, where otherwise it proves only that the serving loop turns.

    ``max_body_bytes`` and ``request_deadline_seconds`` are REQUIRED for the reason ``docs_enabled`` is: every
    lakehouse service sets its own body cap and deadline, so a new one answers both rather than inheriting none.

    ``inner_middleware`` are ``http`` middleware functions that must run INSIDE every other layer, and
    ``outer_middleware`` the caller's pure-ASGI layers, which run inside the deadline and outside the body cap
    (see the module docstring for the whole order).

    ``audit_enabled`` arms the `lance.audit` compliance stream, and is REQUIRED for the same reason
    ``docs_enabled`` beside it is: a keyword with no default is enforced at every call site by the type
    checker, so a new lakehouse service has to ANSWER rather than inherit. `governed/audit.py` gates
    the stream by the dedicated logger's LEVEL, so a service that never calls `configure_audit` leaves
    it at NOTSET and follows whatever `RASK_LOG_LEVEL` set — which meant the volume lever doubled as a
    compliance lever. Three of this factory's four callers armed it in their own lifespans and two did
    not; it was missing where it matters most, on `services/maintenance`, the component that rewrites
    bytes.
    """
    setup_logging()
    configure_audit(enabled=audit_enabled)
    _forbid_migrations_on_read()
    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        servers=[{"url": _SERVED_BESIDE_THE_SCHEMA}],
    )
    if docs_enabled:
        _mount_docs(app)
    register_handlers(app)
    install_problem_handlers(app, log)
    # Each layer added is OUTER to those added before it: innermost first.
    for middleware in inner_middleware:
        app.middleware("http")(middleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    for layer in outer_middleware:
        app.add_middleware(layer.cls, *layer.args, **layer.kwargs)
    app.add_middleware(RequestDeadlineMiddleware, seconds=request_deadline_seconds, log=log)
    app.add_middleware(PanicCatcherMiddleware, log=log)
    app.add_middleware(RequestIDMiddleware)
    for router in routers:
        app.include_router(router)
    app.include_router(make_probes_router(ready_check, live_check))
    return app
