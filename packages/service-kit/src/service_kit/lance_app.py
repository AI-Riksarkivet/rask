"""THE lance-plane app factory — one assembly for catalog, lineage, medallion and maintenance.

Three factories, three planes, and the split is deliberate rather than accidental:

* ``service_kit.make_service_app`` — the FLEET plane (compute, controlplane, ingest, flows,
  notifications). Routers mount under ``RASK_API_PREFIX``; config is ``service_kit.config.Settings``.
* ``service_kit.media.app.build_media_app`` — the MEDIA plane (viewer, search, annotator). Routers
  mount at the root; config is ``MediaSettings``; CORS must expose the Range headers a browser needs
  to seek video.
* this — the LANCE plane. Routers mount at the root under each service's own paths (``/v1/...``,
  ``/produce``, ``/movers/...``), config is each service's own ``*Settings``, and the middleware order
  is per-service because the catalog's Arrow-IPC data plane carries load-shedding and a body cap that
  the others do not.

docs/DECISIONS.md "The Python estate audit" DUP-12 counted eight module-level ``app = FastAPI(...)`` entrypoints hand-assembling
the same boot and asked for a ``make_lance_service_app``. The media three are gone (they build through
``build_media_app`` now); this is the other five, and what it owns is the part that was genuinely
identical in all of them:

1. ``setup_logging()`` BEFORE the app exists. Every module in these services uses
   ``getLogger(__name__)``, and without this they propagate to a root logger with no handlers and are
   DISCARDED — which is not hypothetical, it hid a two-day lineage feed outage.
2. The ``FastAPI`` construction with docs OPT-IN. ``/docs``, ``/redoc`` and ``/openapi.json`` are all
   gated on the service's own ``docs_enabled``; a service that sets none of the three serves all
   three, which is how the schemas shipped openly once.
3. ``register_handlers`` THEN ``install_problem_handlers``. ``service_kit.exceptions.DomainError``
   subclasses ``HTTPException``, so without the first, starlette's built-in handler renders it —
   status and headers intact, ``{"detail": ...}`` body — which is how the draining 503 came to declare
   problem+json over a body that was not one. Registered FIRST so the lance translator still wins for
   ``RequestValidationError``, exactly the order ``make_service_app`` uses. Getting this pair the
   wrong way round is silent: both orders answer, and only one answers correctly.
4. ``RequestIDMiddleware``. One id per request, minted or echoed, published to the context var
   ``setup_logging``'s filter reads — so a caller can quote an id from a failed request and an
   operator can grep for it. PURE ASGI, so it passes streaming bodies through untouched, which is what
   makes it safe on a plane that serves Arrow IPC. The medallion MOVER had no request-id layer at all
   before this factory; that is the drift five copies produced.
5. ``/livez`` + ``/readyz`` at the ROOT — a kubelet does not know a service's prefix.

WHAT IT DOES NOT OWN, and why. ``inner_middleware`` is the escape hatch, and it exists for exactly
one reason: middleware added LATER is OUTER, and the catalog's read-only maintenance gate must sit
INSIDE the request-id layer or its 503 goes out without an ``X-Request-ID`` header — a refusal an
operator cannot then correlate. Middleware that must be OUTER (the catalog's body cap and its
write-concurrency shed) is added by the caller after this returns, which is where it already was.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import APIRouter, FastAPI

from service_kit.app import setup_logging
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.middleware import RequestIDMiddleware
from service_kit.probes import ReadyCheck, make_probes_router


def build_lance_service_app(
    *,
    title: str,
    version: str = "0.1.0",
    docs_enabled: bool,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]],
    log: logging.Logger,
    routers: Sequence[APIRouter] = (),
    ready_check: ReadyCheck | None = None,
    inner_middleware: Sequence[Callable[[Any, Any], Any]] = (),
) -> FastAPI:
    """Assemble one lance-plane service's FastAPI app. See the module docstring for what and why.

    ``log`` is the SERVICE's logger, not this module's: the problem handlers log an unhandled error
    with a traceback, and that record must carry the name of the service it happened in.

    ``inner_middleware`` are ``http`` middleware functions that must run INSIDE the request-id layer
    (see the module docstring). Everything else the caller adds after this returns, which keeps it
    outer — the order those middlewares already had.
    """
    setup_logging()
    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    register_handlers(app)
    install_problem_handlers(app, log)
    for middleware in inner_middleware:
        app.middleware("http")(middleware)
    app.add_middleware(RequestIDMiddleware)
    for router in routers:
        app.include_router(router)
    app.include_router(make_probes_router(ready_check))
    return app
