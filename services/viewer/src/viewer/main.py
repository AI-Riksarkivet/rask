"""Viewer service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in the lifespan onto ``app.state`` (importing this module does
zero I/O). The lifespan and the assembly are both SHARED — ``service_kit.media.lifespan`` and
``service_kit.media.app`` — because viewer, search and annotator are three deployments of one shape
and used to hand-write it three times (docs/DECISIONS.md "The Python estate audit" DUP-16 / X12 / DUP-20).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from service_kit import setup_logging
from service_kit.media.app import build_media_app
from service_kit.media.config import MediaSettings, get_settings
from service_kit.media.lifespan import make_media_lifespan, make_media_state
from service_kit.media.state import AppState
from service_kit.obs import configure_app_logging
from viewer.api.v1.router import router as api_router
from viewer.core.config import get_viewer_settings


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)

# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()


#: `closes` names the resources THIS service opens and must therefore close. `AppState` carries slots
#: for the whole media plane; the viewer fills the http pool and (lazily, on first use) the two model
#: clients, and closing a slot it never filled would read as ownership it does not have.
def _settings() -> MediaSettings:
    """Read the settings AT LIFESPAN TIME, never at import.

    A named indirection rather than passing `get_viewer_settings` itself: resolving the module global on
    each call is what lets a test drive the real lifespan against its own settings, and what keeps
    the process's configuration from being decided by this module's import order.
    """
    return get_viewer_settings()


lifespan = make_media_lifespan(
    _settings,
    service="viewer",
    closes=lambda state: (state.http, state.embedder, state.reranker),
)

app = build_media_app(
    title="lance-media viewer",
    settings=get_viewer_settings(),
    routers=[api_router],
    lifespan=lifespan,
)


def run() -> None:
    import uvicorn

    s = get_viewer_settings()
    uvicorn.run("viewer.main:app", host=s.host, port=s.service_port)


def create_viewer_state(settings: MediaSettings | None = None) -> AppState:
    """Standalone viewer state — registry + HTTP pool (the test/tooling seam)."""
    return make_media_state(settings or get_settings())


def create_viewer_app(settings: MediaSettings | None = None, state: AppState | None = None) -> FastAPI:
    """Viewer app around shared or standalone state — the test/composition seam.

    THE SAME BUILDER production uses (X12). This used to construct its own bare ``FastAPI`` with
    ``register_handlers`` and the middleware only: no ``install_problem_handlers``, no probes — so an
    app built here could not reproduce the one regression the deployed app's comments are about (an
    ``UnauthenticatedError`` answering 500 instead of 401). No lifespan, because the caller supplies
    the state this app runs over.
    """
    resolved = settings or get_settings()
    test_app = build_media_app(title="viewer api", settings=resolved, routers=[api_router])
    test_app.state.resources = state if state is not None else create_viewer_state(resolved)
    return test_app
