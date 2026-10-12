"""services/ingest — pre-bronze acquisition as a platform plane.

Composed through `service_kit.make_service_app` like every other fleet member, so config, error
handlers, middleware, OTel and the Dapr client come from one place (rask-architecture's
entrypoint-over-package contract) rather than being re-assembled here.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from ingest.api import router as ingest_router
from ingest.dapr_workflow import DaprWorkflowReader, DaprWorkflowStarter, DaprWorkflowTerminator, start_runtime, stop_runtime
from ingest.health import router as health_router
from ingest.nats_user import install_nats_user, withdraw_nats_user
from ingest.provenance import LineageProvenanceReader
from ingest.queue_health import router as queue_health_router
from ingest.runs import InMemoryRunStore
from ingest.signing import start_signing, stop_signing
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.signing_key import signing_ready_check
from service_kit.lakehouse.ns_errors import PROBLEM_JSON


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import FastAPI


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the ingest app.

    The run store and the workflow starter hang off `app.state` rather than being imported at module
    scope, so a test can substitute either without a live daprd — which is what keeps A1/A2 fast unit
    tests instead of requiring a cluster to assert a contract that is pure request handling.
    """
    # Populates the SourceAdapter registry by import (I1). A deliberate import-time side effect — the
    # alternative is a hand-maintained list elsewhere, which is exactly the drift the registry exists
    # to prevent.
    from ingest.adapters import register_builtin_sources
    from service_kit import make_service_app

    register_builtin_sources()

    app = make_service_app(
        title="ingest",
        routers=[health_router, queue_health_router, ingest_router],
        lifespan=_lifespan,
        ready_check=signing_ready_check(),
    )
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_starter = DaprWorkflowStarter()
    app.state.workflow_reader = DaprWorkflowReader()
    app.state.workflow_terminator = DaprWorkflowTerminator()
    app.state.provenance_reader = LineageProvenanceReader()
    # The incremental poll (TRIGGER-2), mounted ONLY when a Dapr cron binding names it. Unmounted by
    # default because a cron route with no cron behind it is a door into starting ingest runs that
    # exists for no reason. Root-mounted by `mount_incremental_cron`: the sidecar delivers an input
    # binding to POST /<component name> at the pod root, so the component name, the env var and the
    # served path are one string.
    from ingest.config import settings
    from ingest.cron import mount_incremental_cron

    binding = settings().cron_binding_name
    if mount_incremental_cron(app, binding):
        logger.info("ingest incremental cron mounted at /%s", binding)
    # FAIL-CLOSED DEFAULTS, so the attributes exist before the lifespan runs: `authorize_ingest`
    # reads them directly and 503s on a missing client, which is the correct answer in the window
    # before startup completes. The BUILD itself is in the lifespan (`attach_auth`) because resolving
    # an unpinned store is I/O and this function is sync — and because constructing an OIDCVerifier
    # fetches discovery, which must never happen merely because something built the app object.
    app.state.fga = None
    app.state.oidc = None
    # A DENIAL MUST BE A 403, NOT A 500 — and `make_service_app` now carries that itself. This used to
    # call `install_problem_handlers(app, logger)` here, because the factory only knew the fleet's own
    # handlers and not the `lance_namespace` typed errors the auth door raises, so an unauthorized
    # ingest answered "Internal Server Error". The factory installs the Lance translator for EVERY app
    # it builds (`service_kit/app.py::_install_ns_problem_handlers`, whose comment records that doing
    # it there rather than here is also what settles docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" X11 — ingest's 422
    # differing
    # from its fleet siblings — by making all of them one shape). A second registration of the same
    # three handlers is dead, so it is gone; only the HTTPException envelope below is ingest's own.
    _install_http_exception_handler(app)
    return app


def _install_http_exception_handler(app: FastAPI) -> None:
    """Render the control API's own refusals in problem+json too — ONE envelope for one router.

    The two translators above cover the estate's typed errors (`DomainError`, `LanceNamespaceError`),
    and neither knows `fastapi.HTTPException`. But the ingest control API's refusals ARE
    HTTPExceptions — an unknown source kind, a refused sizing, an idempotency conflict, a busy
    workflow engine, an unknown run — and starlette's default handler served every one of them as
    `application/json` `{"detail": …}`. So a client of a single router had to parse two unrelated
    shapes and could not tell in advance which it would get.

    Four keys and `about:blank#`, matching `service_kit.exceptions`' own `DomainError` body rather
    than the six-key Lance one: a run is not a Lance-Namespace object, and claiming a numeric spec
    code for it would tell a generated client this response belongs to a contract it does not.
    That is the same line `services/gateway` draws for its proxy refusals.

    `exc.headers` is forwarded because the 503 is only actionable WITH its `Retry-After` — a handler
    that builds its own response drops them silently otherwise, which is the defect
    `service_kit.exceptions` had to fix in its own handler.
    """
    from http import HTTPStatus

    from fastapi import Request
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _problem_json(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = int(exc.status_code)
        phrase = HTTPStatus(code).phrase if code in tuple(HTTPStatus) else "Error"
        slug = HTTPStatus(code).name.lower() if code in tuple(HTTPStatus) else "error"
        return JSONResponse(
            status_code=code,
            media_type=PROBLEM_JSON,
            content={
                "type": f"about:blank#{slug}",
                "title": phrase,
                "status": code,
                "detail": exc.detail if isinstance(exc.detail, str) else phrase,
            },
            headers=exc.headers,
        )


def _lifespan(settings: Any) -> Any:  # noqa: ANN401 — service_kit's LifespanFactory shape
    """Start the Dapr WorkflowRuntime for the lifetime of the process.

    THE WORKER. Without this the service can schedule a workflow and nothing will ever execute it:
    `DaprWorkflowClient` only enqueues, and the runtime is what registers the definitions and pulls
    work. The first in-cluster deploy had the engine running in the sidecar
    (`Workflow engine started`) and still could not run a workflow, because the APP side was absent —
    an asymmetry that looks healthy from every angle except an actual run.

    Registration lives in `ingest.workflow.register()` so there is ONE list of workflows and
    activities. A definition registered in the API process but not the worker fails at runtime with
    an unhelpful "no such activity", which is the failure mode that argues for a single registry.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # `provision=False` — now the estate-wide default, kept explicit here because this is the door
        # where it is most load-bearing. A data writer that mints a store or writes an authorization
        # model becomes the source of truth for everyone else's permissions, and that is not ingest's
        # job. Reading which store the estate already uses is not authoring one — the distinction
        # ingest originally missed, which made it the only service that could not boot on the chart's
        # own default (`auth.fgaStoreId: ""`, a per-cluster ULID that cannot be a committed default).
        # It still fails closed: no store to resolve means no client, and the door 503s.
        #
        # Non-fatal, deliberately: OpenFGA being slow to accept connections is an ordering blip, not
        # a reason to CrashLoopBackOff.
        from ingest.auth import get_auth_settings

        await attach_auth(app, get_auth_settings(), service="ingest", provision=False)
        # INGEST'S OWN NATS USER ([[XC-078]]), read through its own sidecar and installed BEFORE the workflow runtime starts,
        # because the worker resumes recovered activities the moment it starts and each of them connects to the broker;
        # withdrawn after the runtime has stopped. A store without the user leaves ingest connecting without one, and a
        # user the store holds that cannot be used refuses the boot.
        await install_nats_user()
        # THIS SERVICE'S OWN SIGNING KEY ([[LH-064]]), resolved through its own sidecar, and installed BEFORE the workflow
        # runtime starts and withdrawn AFTER it has shut down. The worker pulls recovered activities the moment it
        # starts and joins the ones in flight when it stops, and an activity that stages a lineage event outside the
        # key's lifetime cannot sign it, so staging nothing loses the event. `start_signing` makes one bounded attempt
        # to resolve the key and does not wait for it to be published, so an absent key does not hold the boot: the
        # probes are served from the moment this lifespan yields, ingest reports itself not ready, and it heals in
        # place when the key is published.
        signing = await start_signing(app)
        try:
            runtime = None
            try:
                runtime = start_runtime()
                app.state.workflow_runtime = runtime
                logger.info("dapr workflow runtime started")
                # The line above is TRUE and INSUFFICIENT: the runtime starts whether or not this
                # app-id can reach an actor state store, and without one the first call fails (and,
                # on dapr 1.18.1, panics the sidecar). Ask the sidecar what it can actually see.
                await probe_actor_state_store(capability="no ingest run can start")
            except Exception:
                # Deliberately non-fatal. The health probe answers process liveness (see health.py), and
                # a service that refuses to start because its sidecar is not up yet turns an ordering
                # blip into a CrashLoopBackOff. Runs will fail loudly at schedule time instead, which is
                # where the operator can actually see them.
                logger.warning("dapr workflow runtime unavailable — runs cannot execute", exc_info=True)
                app.state.workflow_runtime = None
            try:
                yield
            finally:
                # Close the OpenFGA client `attach_auth` opened. The SDK is aiohttp-backed, so an unclosed
                # client leaks one half-open connection per replica (plus an "Unclosed client session" on
                # the way out); `fga.dispose` is None-safe and suppress-wrapped, so it is safe whether or
                # not auth was wired.
                from service_kit.governed import fga

                await fga.dispose(app)
                if runtime is not None:
                    stop_runtime(runtime)
        finally:
            await stop_signing(signing)
            withdraw_nats_user()

    return lifespan
