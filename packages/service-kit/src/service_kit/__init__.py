"""Shared factory for rask backend services (was backends/_common.py).

`make_service_app` builds a FastAPI app with shared config, exception handlers,
middleware, and logging. The lifespan is injectable: stateless services get the
minimal `default_lifespan` (settings only); services needing resources (DB, Lance,
Ray, S3) pass their own factory, e.g. `core.lifespan.make_lifespan`.
"""

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI

from service_kit.config import Settings
from service_kit.context import RequestIdFilter
from service_kit.exceptions import register_handlers
from service_kit.middleware import register_middleware
from service_kit.otel import setup_otel
from service_kit.probes import ReadyCheck, make_probes_router
from service_kit.slash import SlashToleranceMiddleware
from storage import derive_hcp_creds


def setup_logging() -> None:
    """Send application logs to stdout — configured on the ROOT logger, so every module inherits it.

    This used to attach the handler to two logger names, ``core`` and ``backends``, and its docstring
    said why: it "mirrors core.main". That monolith is gone. Every module in every service now uses
    ``logging.getLogger(__name__)`` — ``lineage.services.consumer``, ``medallion.services.transform``,
    ``catalog.api.v1.endpoints.tables`` — and NONE of those names sit under ``core.*`` or
    ``backends.*``. So the handler decorated two trees nobody logs to, every record propagated to a
    root logger with no handlers, and was discarded. Only uvicorn's own logs (which carry their own
    handlers) ever reached stdout, which is exactly why the estate looked quiet while it was not.

    Measured live 2026-08-17, and verified three times against the running service:

    * a malformed event POSTed to the running ingest answered ``{"status":"DROP"}`` — returned on the
      single line immediately AFTER ``log.error("lineage_event_invalid")``, so the branch demonstrably
      ran — and produced no log line at all. After this fix the identical request logs
      ``ERROR lineage.services.consumer — lineage_event_invalid``;
    * the medallion mover's ``ray_stage_job_submitted`` and ``medallion_stage_workflow_reattach`` were
      equally invisible, which is why a running cascade read as an idle one and its Dapr Workflow had
      to be found by querying the sidecar rather than by reading a log.

    The class this closes is the swallowed diagnostic: ``record_event_best_effort`` catches a feed
    write failure by design (it must not break the authoritative graph write) and reports it with a
    WARNING. That warning had nowhere to go, so the one signal distinguishing "the feed is fine" from
    "the feed has been failing" did not exist.

    ``RASK_LOG_LEVEL`` is the volume lever (default INFO). Root-level INFO does make chatty
    third-party libraries audible; that is the correct trade against losing every application log,
    and it is one env var to turn down rather than a hardcoded allowlist that goes stale the same way
    this one did.
    """
    level = os.environ.get("RASK_LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent by handler NAME, not by count: `make_service_app` runs this per app, and a duplicate
    # handler emits every line twice.
    if not any(h.get_name() == "rask-stdout" for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("rask-stdout")
        # THE REQUEST ID JOINS EVERY RECORD HERE. `RequestIDMiddleware` mints one and echoes it to the
        # caller; until this filter existed nothing read it, so a user could quote an id from a failed
        # request and an operator had nothing to grep for.
        #
        # A FILTER on the one handler, not a `LoggingMiddleware`: a middleware annotates only the
        # records it writes itself, while this covers every module in every service — libraries
        # included — with no per-service edit. Outside a request it renders `-`, which is why the
        # context var defaults to a visible placeholder rather than an empty string that would read
        # like a formatting bug.
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] — %(message)s"))
        root.addHandler(handler)


def build_settings() -> Settings:
    load_dotenv()
    derive_hcp_creds()
    return Settings.model_validate({})


# THE DAPR CLIENT SEAM IS DELETED. It built `DaprClient("http://127.0.0.1:3500")`
# — the sidecar's HTTP port handed to a gRPC client, which talks 50001 — and a test pinned that wrong
# constant as if it were the contract. Two re-verifications found the same thing: NOTHING called it.
# Every service that actually publishes builds its own client (`medallion.mover` / `medallion.producer`
# construct `dapr.aio.clients.DaprClient` in their own lifespans, with their own `get_dapr` and
# `DaprClientDep` in `medallion.api.dependencies`), and the four `make_service_app` services never
# touched `app.state.dapr` at all.
#
# So the wrong port was inert — but a broken seam that looks available is worse than no seam: the next
# service to want a Dapr client would have reached for the one already wired into the app factory and
# got a client pointed at the wrong port. Deleted rather than repaired to the gRPC port, because
# repairing it would keep a shared abstraction alive that no caller has ever wanted.


def default_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Minimal lifespan for stateless services: expose `settings` on `app.state`.

    Services that need resources (DB, Lance, Ray, S3) inject their own factory.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        yield

    return lifespan


type LifespanFactory = Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]


def _install_ns_problem_handlers(app: FastAPI) -> None:
    """Install the Lance-Namespace problem-detail translators, IF this deployable has them.

    THE IMPORT IS DEFERRED, and that is a packaging constraint rather than a performance one.
    `lance_namespace` lives behind service-kit's `[governed]` / `[lakehouse]` extras and MUST stay
    there: this library is shared by every service including the storeless ones, so a Lance dependency
    in its base is a Lance dependency in the gateway. Importing it at module scope made
    `import service_kit` itself require the extra, which is how the gateway image stopped building at
    all — `ModuleNotFoundError: No module named 'lance_namespace'`, caught by `.docker/import-gate.py`
    while every test stayed green (the workspace venv resolves the extra through a sibling member).

    SKIPPING IS SOUND, NOT A SILENT DEGRADATION, and this is the load-bearing argument. These handlers
    translate `lance_namespace` EXCEPTION TYPES. If the package is not importable then those classes do
    not exist in this process, so no code in it can raise one — and the governed kernel that raises them
    (`service_kit.governed.fga`) sits behind the very same extra, so it is absent too. There is nothing
    left for the handler to catch. A deployable that DOES serve that surface declares the extra and gets
    the handlers exactly as before.

    A missing `lance_namespace` is tolerated; anything else is re-raised. A broken import INSIDE
    `ns_errors` must not be swallowed as "the extra is absent" — that would turn a real defect into a
    quietly unhandled error class.
    """
    try:
        from service_kit.lakehouse.ns_errors import install_problem_handlers
    except ModuleNotFoundError as exc:
        if exc.name != "lance_namespace":
            raise
        logging.getLogger(__name__).debug(
            "lance_namespace absent — Lance-Namespace problem handlers not installed; nothing in this deployable can raise those types"
        )
        return
    install_problem_handlers(app, logging.getLogger(__name__))


def make_service_app(
    *,
    title: str,
    routers: Sequence[APIRouter],
    proxy_router: APIRouter | None = None,
    lifespan: LifespanFactory | None = None,
    ready_check: ReadyCheck | None = None,
) -> FastAPI:
    """Build a backend FastAPI app with shared config/handlers/middleware.

    `routers` are mounted under `settings.api_prefix`; `proxy_router` (the Ray
    Serve proxy) is mounted at the root, matching `core.main`. The lifespan
    defaults to the minimal `default_lifespan` unless `lifespan=` is passed.
    """
    setup_logging()
    settings = build_settings()
    base_factory: LifespanFactory = lifespan if lifespan is not None else default_lifespan

    def lifespan_factory(s: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
        base = base_factory(s)

        @asynccontextmanager
        async def wrapped(app: FastAPI) -> AsyncIterator[None]:
            # Nothing is wrapped around the base lifespan any more — `app.state.dapr` used to be built
            # here for every service and read by none (§2.1). Kept as a wrapper rather than collapsed to
            # `base_factory` so the ONE place a cross-cutting startup concern belongs still exists.
            async with base(app):
                yield

        return wrapped

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=lifespan_factory(settings),
        # Opt-IN, and it used to be hard-coded on with no way for a service to say no. See
        # `Settings.docs_enabled` for why the default is closed rather than open.
        docs_url=f"{settings.api_prefix}/docs" if settings.docs_enabled else None,
        redoc_url=f"{settings.api_prefix}/redoc" if settings.docs_enabled else None,
        openapi_url=f"{settings.api_prefix}/openapi.json" if settings.docs_enabled else None,
    )
    register_handlers(app)
    # AND the lance_namespace translator, because the governed kernel these apps call raises those
    # types: `governed/fga.py` raises `ServiceUnavailableError` on an FGA outage under a docstring
    # promising "outage → ServiceUnavailableError → 503". Without this the fleet apps could not map
    # one, so that outage answered a text/plain 500 — monitoring cannot tell a dependency being down
    # from this service being broken, and `notifications/api/visibility.py` answered the same class of
    # failure two different ways inside ONE function.
    #
    # `ingest/__init__.py` already did exactly this on top of the factory, with the reason in a
    # comment ("A DENIAL MUST BE A 403, NOT A 500"). Doing it HERE instead of per app also settles the
    # divergence open_python-audit X11 files — ingest's 422 body differing from its three fleet
    # siblings — by making all of them one shape rather than by removing what made ingest right.
    _install_ns_problem_handlers(app)
    register_middleware(app, settings)

    # THE OPERATIONAL PROBES, root-mounted, for every app this factory builds.
    #
    # `service_kit.probes` exists so the estate has ONE drain-aware `/livez` + `/readyz` pair — its own
    # docstring records that the same twenty lines had already drifted three ways across six hand-rolled
    # copies. This factory then mounted it nowhere, so compute, controlplane, flows and ingest served
    # neither probe at all and only notifications root-mounted it by hand.
    #
    # ROOT, not under `api_prefix`: a kubelet does not know a service's prefix. Unconditional because
    # the router is dependency-free — `ready_check` is the optional half, for a service with a hard
    # dependency worth reporting (notifications' actor plane).
    #
    # `/api/health` is untouched: it is the frontend-facing badge the estate documents it to be. The
    # point is that it is not a READINESS answer — it is a constant, so probing it reports "ready"
    # while the pod drains.
    app.include_router(make_probes_router(ready_check))

    for router in routers:
        app.include_router(router, prefix=settings.api_prefix)
    if proxy_router is not None:
        app.include_router(proxy_router)

    # Dapr drops trailing slashes; resolve the variant in-process instead of a
    # 307 redirect that would leak the service's in-pod address (see slash.py).
    app.router.redirect_slashes = False
    app.add_middleware(SlashToleranceMiddleware, routes_provider=lambda: app.router.routes)

    setup_otel(app, service_name=title, settings=settings)

    return app
