"""Search service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). The lifespan and the assembly are both SHARED — ``service_kit.media.lifespan`` and
``service_kit.media.app`` — because viewer, search and annotator are three deployments of one shape
and used to hand-write it three times (open_python-audit DUP-16 / X12 / DUP-20).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from search.api.v1.router import router as api_router
from search.core.config import get_search_settings
from search.core.rate_limit import limiter
from service_kit import setup_logging
from service_kit.media.app import build_media_app
from service_kit.media.config import MediaSettings, get_settings
from service_kit.media.lifespan import make_media_lifespan
from service_kit.media.state import AppState
from service_kit.obs import configure_app_logging
from service_kit.rate_limit import register_rate_limiting


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)

# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()


def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_search_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_search_settings()


#: `attach_auth` inside the shared lifespan IS THE LINE THAT MAKES THE GATE FUNCTION. `api/security.py`
#: builds the deps and every route declares them, but `make_auth_deps.get_checker` is fail-CLOSED: FGA
#: enabled with no client on `app.state` is a 503, not a permissive fallback. Without it the whole X6
#: seam — settings mixin, gated routes, chart env — resolves to "Authorization is enabled but
#: unavailable" on every request, which is safe and is also the service doing nothing. It shipped that
#: way three times (compute, controlplane, search); hence
#: `tests/unit/test_governed_services_wire_their_gate.py`, and hence one lifespan rather than three.
lifespan = make_media_lifespan(
    _settings,
    service="search",
    closes=lambda state: (state.http, state.embedder, state.reranker),
)


def _with_rate_limiting(app: FastAPI) -> FastAPI:
    """PER-ROUTE rate limiting for the GPU surfaces (see search.core.rate_limit).

    Applied here rather than inside `build_media_app` because it is the search plane's alone: slowapi
    reads `app.state.limiter` by that exact name for its header injection, and the problem+json 429
    replaces slowapi's default so a refusal is shaped like every other error in the estate. It runs
    AFTER the shared handlers for the same reason it always did — its 429 handler is the last word on
    that status.
    """
    register_rate_limiting(app, limiter)
    return app


app = _with_rate_limiting(
    build_media_app(
        title="lance-media search",
        settings=get_search_settings(),
        routers=[api_router],
        lifespan=lifespan,
    )
)


def run() -> None:
    import uvicorn

    s = get_search_settings()
    uvicorn.run("search.main:app", host=s.host, port=s.service_port)


def create_search_app(state: AppState, settings: MediaSettings | None = None) -> FastAPI:
    """Search app around externally-opened state — the test/composition seam.

    THE SAME BUILDER production uses (X12). This used to construct a bare ``FastAPI`` with
    ``register_handlers`` and nothing else — no ``install_problem_handlers``, no middleware, no body
    cap, no probes — so an app built here could not reproduce the one regression the deployed app's
    comments are about (an ``UnauthenticatedError`` answering 500 instead of 401), and its 429s were
    shaped differently too. No lifespan, because the caller supplies the state this app runs over.
    """
    test_app = _with_rate_limiting(build_media_app(title="search api", settings=settings or get_settings(), routers=[api_router]))
    test_app.state.resources = state
    return test_app
