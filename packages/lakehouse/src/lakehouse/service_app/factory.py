"""THE lakehouse's app factory — one assembly for catalog, lineage and maintenance.

Routers mount at the root under each service's own paths (``/v1/...``, ``/events``),
config is each service's own ``*Settings``, and every layer a request passes through is installed here, in one order.

It serves catalog, lineage and maintenance (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md, DUP-12),
and what it owns is the part of the boot that is identical in all three:

1. ``setup_logging()`` BEFORE the app exists. Every module in these services uses
   ``getLogger(__name__)``, and without this they propagate to a root logger with no handlers and are
   DISCARDED — which is not hypothetical, it hid a two-day lineage feed outage.
2. The ``FastAPI`` construction with docs OPT-IN. ``/docs``, ``/redoc`` and ``/openapi.json`` are all
   gated on the service's own ``docs_enabled``; a service that sets none of the three serves all
   three, which is how the schemas shipped openly once. Each page names its schema by a URL relative to itself, and
   the schema its server the same way (``_SERVED_BESIDE_THE_SCHEMA``): a service is published under a prefix its route
   rewrites or the service strips, which it is never told, so only a relative URL is right behind every route.
3. ``install_problem_handlers``, the one handler set: every failure a door answers, a raised ``lance_namespace``
   error, a request that fails validation, the framework's own HTTP errors and a crash, is the spec's
   ``ErrorResponse`` body with its ``code`` (`lakehouse.problems.ns_errors`).
4. The layers every request passes, outermost first (ADR 0203):

   * OpenTelemetry's server span (:func:`_trace_every_request`): every request runs in a trace, the caller's
     ``traceparent`` continued where one is sent, so its id is the one correlation id (ADR 0209) the log lines, the
     audit trail, the problem body's ``instance``, the commits and the events it causes all carry. OUTERMOST, so every
     refusal a layer below answers runs in it.
   * ``NoStoreMiddleware``: every answer carries ``Cache-Control: no-store`` unless its door named its own caching, and
     varies on ``Authorization``, since a door may answer the credentials it vends (`lakehouse.service_app.no_store`). It wraps
     the app's own entry point (:class:`_UncachedApp`), so the server's error layer is inside it: an unexpected
     exception's 500 is answered by that error layer, outside every layer ``add_middleware`` installs.
   * ``PanicCatcherMiddleware``: a native library's panic answers 500 with the problem body and the process
     serves on.
   * ``RequestDeadlineMiddleware``: a request with no answer begun within ``request_deadline_seconds`` is
     answered 503 while its door runs on to its end. Outside the caller's ``outer_middleware``, so a door
     running on past its deadline still holds the write seat the catalog's shed counted it in.
   * ``outer_middleware``, the caller's own, in its order (the catalog's path reading and write shed).
   * ``BodySizeLimitMiddleware`` at ``max_body_bytes``, a body over it refused 413 before it is buffered.
   * ``inner_middleware``, the caller's ``http`` functions that must run INSIDE every other layer: the
     catalog's read-only gate and signing gate answer inside the request's trace this way.
5. ``/livez`` + ``/readyz`` at the ROOT — a kubelet does not know a service's prefix.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, override

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from lakehouse.governed.audit import configure_audit
from lakehouse.problems.ns_errors import install_problem_handlers
from lakehouse.process.draining import arm_drain_on_sigterm
from lakehouse.process.log_handler import setup_logging
from lakehouse.process.probes import LiveCheck, ReadyCheck, make_probes_router
from lakehouse.service_app import thread_pool
from lakehouse.service_app.body_limit import BodySizeLimitMiddleware
from lakehouse.service_app.deadline import RequestDeadlineMiddleware
from lakehouse.service_app.no_store import NoStoreMiddleware
from lakehouse.service_app.panic_catcher import PanicCatcherMiddleware


#: A Starlette ``http`` middleware function: the request, and the rest of the app to call with it.
type HttpMiddleware = Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]

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


class _UncachedApp(FastAPI):
    """A FastAPI app whose every answer passes `NoStoreMiddleware`, the catch-all handler's among them.

    Starlette answers an unhandled exception from its own error layer, which it builds outside every layer
    ``add_middleware`` installs, so a layer added that way never sees that answer. The layer therefore goes around the
    app's own entry point, and not into the stack the app builds: OpenTelemetry's instrumentation wraps that stack and
    refuses one whose outermost layer is not Starlette's error layer (measured on the locked instrumentation: it logs
    "Skipping FastAPI instrumentation due to unexpected middleware stack" and traces nothing).
    """

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await NoStoreMiddleware(super().__call__)(scope, receive, send)


def _trace_every_request(app: FastAPI) -> None:
    """Run every request of ``app`` in a trace, so each has the one correlation id (ADR 0209).

    A process `opentelemetry-instrument` launched already holds an SDK provider and instruments every FastAPI app it
    builds (the mark the instrumentation sets on the app, read here, so a second call does not log its warning). A
    process launched without it (the chart's with `observability.enabled` off, a test) holds the API's no-op provider,
    under which a request with no ``traceparent`` of its own has no trace id at all; it gets an SDK provider with no
    exporter, which mints ids and sends nothing anywhere. The provider is the process's, set once whichever app is built
    first.
    """
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    # The instrumentation's own mark on the app, which its `instrument_app` reads the same way (measured on 0.66b2). It
    # offers no public answer for one app: the instrumentor's `is_instrumented_by_opentelemetry` is the process's, and
    # says nothing of an app whose class was imported before `instrument()` ran.
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(app)


def _served(lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]]) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """``lifespan``, entered once the serving loop's thread pool is reported (`lakehouse.service_app.thread_pool`), with the drain
    armed while it serves.

    ARMED AT SIGTERM, for every service the factory builds (`lakehouse.process.draining.arm_drain_on_sigterm`). A lifespan's own
    ``finally`` runs only after uvicorn has stopped accepting connections and drained, so a probe reading a flag set
    there answers ready through the whole drain, and Kubernetes sends SIGTERM at the START of termination. Armed once
    the service's lifespan has started, and restored before it is torn down, so the flag is set while everything the
    service holds is still open. A process that serves this app with no lifespan (a worker answering its probes from a
    thread) takes SIGTERM itself.
    """

    @asynccontextmanager
    async def serving(app: FastAPI) -> AsyncGenerator[None]:
        thread_pool.watch()
        async with lifespan(app):
            disarm = arm_drain_on_sigterm(app)
            try:
                yield
            finally:
                disarm()

    return serving


def _mount_docs(app: FastAPI) -> None:
    """Serve ``/docs`` and ``/redoc``, each fetching the schema beside itself."""

    async def swagger() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=_SCHEMA_BESIDE_THE_PAGE, title=f"{app.title} - Swagger UI")

    async def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url=_SCHEMA_BESIDE_THE_PAGE, title=f"{app.title} - ReDoc")

    app.add_api_route("/docs", swagger, methods=["GET"], include_in_schema=False)
    app.add_api_route("/redoc", redoc, methods=["GET"], include_in_schema=False)


def build_service_app(
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
    inner_middleware: Sequence[HttpMiddleware] = (),
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
    it at NOTSET and follows whatever `RASK_LOG_LEVEL` set, so the volume lever would double as a
    compliance lever. Arming it here, for all three callers (catalog, lineage, maintenance), holds it on
    `services/maintenance` too, the component that rewrites bytes.
    """
    setup_logging()
    configure_audit(enabled=audit_enabled)
    _forbid_migrations_on_read()
    app = _UncachedApp(
        title=title,
        version=version,
        lifespan=_served(lifespan),
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        servers=[{"url": _SERVED_BESIDE_THE_SCHEMA}],
    )
    if docs_enabled:
        _mount_docs(app)
    install_problem_handlers(app, log)
    # Each layer added is OUTER to those added before it: innermost first.
    for middleware in inner_middleware:
        app.middleware("http")(middleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    for layer in outer_middleware:
        app.add_middleware(layer.cls, *layer.args, **layer.kwargs)
    app.add_middleware(RequestDeadlineMiddleware, seconds=request_deadline_seconds, log=log)
    app.add_middleware(PanicCatcherMiddleware, log=log)
    _trace_every_request(app)
    for router in routers:
        app.include_router(router)
    app.include_router(make_probes_router(ready_check, live_check))
    return app


__all__ = ["build_service_app"]
