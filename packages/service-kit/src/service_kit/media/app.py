"""THE media-service app factory — one assembly for viewer, search and annotator, prod AND test.

``service_kit.make_service_app`` is the FLEET factory: it mounts routers under ``RASK_API_PREFIX``,
registers the fleet middleware stack and reads ``service_kit.config.Settings``. The media trio is a
different plane — root-mounted routers, its own ``MediaSettings``, and a CORS policy that must expose
``Content-Range``/``Accept-Ranges`` so a browser can seek video — so it cannot use that factory
without either lying about its config or bending the fleet's. This is the media plane's equivalent,
and it exists for the same reason: three ``main.py`` files were hand-assembling the same boot.

It also closes open_python-audit **X12**. ``create_viewer_app`` / ``create_search_app`` are the
declared test/composition seam, and they built a DIFFERENT app from the deployed one — search's had
no ``install_problem_handlers`` at all, neither had the probes, neither had the body cap. The exact
regression both mains' comments describe (an ``UnauthenticatedError`` from the OIDC verifier escaping
unmapped and answering 500 instead of 401) is invisible to a test that goes through the seam. Both
now build through this one function, so the seam and the deployment cannot disagree about what an
error looks like.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI

from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.media.config import MediaSettings
from service_kit.media.middleware import register_media_middleware
from service_kit.probes import ReadyCheck, make_probes_router


log = logging.getLogger(__name__)


def build_media_app(
    *,
    title: str,
    settings: MediaSettings,
    routers: Sequence[APIRouter],
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    ready_check: ReadyCheck | None = None,
) -> FastAPI:
    """Assemble one media service's FastAPI app.

    Order is load-bearing and is the order all three mains had converged on:

    * **Docs are OPT-IN** (``MEDIA_DOCS``). The three constructors used to set none of the three URLs,
      so FastAPI's defaults stood and ``/docs``, ``/redoc`` and ``/openapi.json`` were all served.
    * ``register_handlers`` FIRST, then ``install_problem_handlers``. ``register_handlers`` maps
      ``DomainError`` (a subclass of ``HTTPException``, which starlette would otherwise render as
      ``{"detail": …}`` under a problem+json content type); the OIDC verifier raises
      ``lance_namespace``'s ``UnauthenticatedError``, which is NOT a ``DomainError``, so without the
      second an expired or wrong-audience bearer answered 500 — which a zone renders as "the service
      is unreachable", sending everyone to look at networking for what is really "sign in again".
      Second wins for ``RequestValidationError``, exactly the order ``make_service_app`` uses.
    * The media middleware stack (CORS with the Range headers exposed, the body ceiling, one request
      id) — see ``service_kit.media.middleware``.
    * The probes at the ROOT: a kubelet does not know a service's prefix. ``ready_check`` is the
      optional half, for a service with a dependency worth reporting (the annotator's actor plane).

    ``lifespan`` is optional so the test/composition seam can build the SAME app around state it
    opened itself, rather than a second, thinner app that maps fewer errors.
    """
    app = FastAPI(
        title=title,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    register_handlers(app)
    install_problem_handlers(app, log)
    register_media_middleware(app, settings)
    app.include_router(make_probes_router(ready_check))
    for router in routers:
        app.include_router(router)
    return app
