"""services/ingest — pre-bronze acquisition as a platform plane.

Composed through `service_kit.make_service_app` like every other fleet member, so config, error
handlers, middleware, OTel and the Dapr client come from one place (rask-architecture's
entrypoint-over-package contract) rather than being re-assembled here.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from ingest.api import router as ingest_router
from ingest.health import router as health_router
from ingest.provenance import LineageProvenanceReader
from ingest.queue_health import router as queue_health_router
from ingest.runs import SCHEDULE_TIMEOUT_SECONDS, InMemoryRunStore
from service_kit.lakehouse.ns_errors import install_problem_handlers


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

    app = make_service_app(title="ingest", routers=[health_router, queue_health_router, ingest_router], lifespan=_lifespan)
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_starter = _DaprWorkflowStarter()
    app.state.workflow_reader = _DaprWorkflowReader()
    app.state.provenance_reader = LineageProvenanceReader()
    # The authz/authn clients the ingest door needs. Wired here rather than lazily inside the
    # dependency because `authorize_ingest` FAILS CLOSED on a missing client — a lazily-absent client
    # would 503 every request instead of authorizing it, and the estate's review found exactly that
    # bypass shape once already (a gate silently off with FGA_ENABLED=true because the client was
    # never built).
    _wire_auth(app)
    # A DENIAL MUST BE A 403, NOT A 500. `make_service_app` carries the fleet's own handlers, which do
    # not know the `lance_namespace` typed errors the auth door raises — so without this an
    # unauthorized ingest answered "Internal Server Error", which tells the caller nothing, tells
    # monitoring the wrong thing, and would have had someone debugging a crash that was the gate
    # working. The same handler every governed service installs; the estate's rule is that endpoints
    # raise typed errors and ONE translator maps all 22 codes.
    install_problem_handlers(app, logger)
    return app


def _wire_auth(app: FastAPI) -> None:
    """Build the FGA client and OIDC verifier onto `app.state`, or leave them None when disabled.

    Same construction shape as the catalog, lineage and medallion consumers — pinned store/model else
    provision, explicit timeout — so all of them behave alike. A per-service variation here is how one
    pod ends up minting a model version on every restart.
    """
    from ingest.auth import get_auth_settings

    settings = get_auth_settings()

    app.state.fga = None
    if settings.fga_enabled:
        from service_kit.governed import fga

        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if store_id and model_id:
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
        # UNPINNED is not a failure here — it is resolved in the lifespan, where there is an event
        # loop (`_resolve_fga_client`). Building nothing and leaving `app.state.fga` None keeps the
        # fail-closed property intact for the window before startup completes.

    app.state.oidc = None
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        from service_kit.governed.oidc import OIDCVerifier

        app.state.oidc = OIDCVerifier(
            settings.oidc_issuer,
            settings.oidc_audience,
            settings.oidc_cache_ttl,
            leeway=settings.oidc_leeway,
            allow_insecure=settings.oidc_allow_insecure,
            # SPLIT-HORIZON DISCOVERY, and the ONLY door in the estate that was missing it. The issuer
            # is the browser-facing URL that lands in a token's `iss` claim (`http://localhost:8080/dex`
            # in k3s); the discovery document has to be fetched from the in-cluster service
            # (`http://rask-dex:5556/dex`). Without the override the verifier fetches discovery from the
            # issuer, which resolves to the POD ITSELF, and every user-bearer ingest died with
            #
            #     httpx.ConnectError: [Errno 111] Connection refused
            #
            # surfaced to the browser as `{"message":"Internal Error"}` — a 500 with nothing about auth
            # in it. The service-token path never touched the verifier, so every in-cluster test passed
            # and only a real signed-in submit from `/compute/etl` reached the line.
            #
            # The chart has been setting `LANCE_OIDC_DISCOVERY_URL` all along and `GovernedAuthSettings`
            # has been parsing it; this door simply never passed it on, while catalog, lineage, viewer,
            # annotator and medallion all did (identical expression, five sites).
            discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
        )


async def _resolve_fga_client(app: FastAPI) -> None:
    """When the store/model are UNPINNED, resolve the one the estate already uses. Read-only.

    THIS PLANE STILL REFUSES TO PROVISION. A data writer that mints a store or writes an authorization
    model becomes the source of truth for everyone else's permissions, and that is not ingest's job.
    But it applied that principle to the LOOKUP as well, and reading which store exists is not
    authoring one — so ingest ended up the only service in the estate that cannot boot on the chart's
    own default posture.

    `auth.fgaStoreId: ""` is that posture, and it is correct: a store id is a per-cluster ULID, so it
    cannot be a committed chart default. Catalog, lineage and medallion resolve by name and come up;
    ingest 503'd every user-bearer request on every dev and e2e cluster. It reaches a person as
    `{"message":"Internal Error"}` from an ETL submit, and the only trace is a startup warning.

    The stopgap was `kubectl set env LANCE_FGA_STORE_ID=…` on the live deployment, which drifts from
    the chart and dies at the next `make k3s-up` — a fix that has to be reapplied by hand is a defect
    wearing a workaround.

    HERE rather than in `_wire_auth` because resolving is I/O and `_wire_auth` is sync. Leaving
    `app.state.fga` None until startup completes keeps the fail-closed window closed: `authorize_ingest`
    503s on a missing client, which is the correct answer before the estate is known to be reachable.

    Still fails closed after: `fga.resolve` returns None when no store or model exists, meaning the
    estate has not been bootstrapped, and the client stays unwired.
    """
    from ingest.auth import get_auth_settings

    settings = get_auth_settings()
    if not settings.fga_enabled or app.state.fga is not None:
        return
    from service_kit.governed import fga

    try:
        resolved = await fga.resolve(settings.fga_api_url)
    except Exception:
        # Never fatal at startup. OpenFGA being slow to accept connections is an ordering blip, not a
        # reason to CrashLoopBackOff; the door 503s until it is reachable, which is where an operator
        # can actually see it.
        logger.warning("ingest: could not reach OpenFGA to resolve a store — the ingest door will 503", exc_info=True)
        return
    if resolved is None:
        logger.warning("ingest: LANCE_FGA_ENABLED but no provisioned OpenFGA store to resolve — the ingest door will 503")
        return
    store_id, model_id = resolved
    app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
    logger.info("ingest: resolved the OpenFGA store by name (unpinned) — pin LANCE_FGA_STORE_ID/MODEL_ID for production")


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
        await _resolve_fga_client(app)
        runtime = None
        try:
            import dapr.ext.workflow as wf

            from ingest.workflow import register

            runtime = wf.WorkflowRuntime()
            register(runtime)
            # start() spawns the worker's own threads; it does not block the event loop.
            runtime.start()
            app.state.workflow_runtime = runtime
            logger.info("dapr workflow runtime started")
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
            if runtime is not None:
                with_suppressed = getattr(runtime, "shutdown", None)
                if callable(with_suppressed):
                    with_suppressed()

    return lifespan


class _DaprWorkflowStarter:
    """Schedules `ingest_run` through the Dapr workflow client in the sidecar.

    Imported lazily inside `start` so constructing the app — which every test does — never requires a
    reachable sidecar.
    """

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        import dapr.ext.workflow as wf

        from ingest.workflow import ingest_run

        def _schedule() -> None:
            client = wf.DaprWorkflowClient()
            client.schedule_new_workflow(workflow=ingest_run, input=payload, instance_id=run_id)

        # to_thread + a timeout: the client is SYNCHRONOUS gRPC, so calling it directly would block
        # the event loop for every other request, and calling it unbounded would hold the POST open
        # past A1's one-second contract. A scheduling failure surfaces as an error on the run rather
        # than a hung connection — a caller that gets no answer cannot retry intelligently.
        await asyncio.wait_for(asyncio.to_thread(_schedule), timeout=SCHEDULE_TIMEOUT_SECONDS)


class _DaprWorkflowReader:
    """Reads a run's live state from the engine, for `GET /v1/ingests/{id}`.

    The run's truth lives in the workflow's durable history, not in this process. Reading it here —
    rather than having activities write back into a local cache — is what makes the status endpoint
    correct after a pod restart and consistent across replicas, and it removes the second writable
    copy of a state that already has an owner.
    """

    def state(self, run_id: str) -> dict[str, object] | None:
        try:
            import dapr.ext.workflow as wf

            state = wf.DaprWorkflowClient().get_workflow_state(run_id, fetch_payloads=True)
        except Exception:
            # No sidecar, or the instance is unknown. The caller falls back to the accepted record;
            # a status endpoint that 500s when the engine is unreachable fails at precisely the
            # moment an operator is using it to find out why.
            logger.debug("workflow state unavailable for run %s", run_id, exc_info=True)
            return None
        if state is None:
            return None
        # `to_json()`, verified against dapr-ext-workflow 1.18.3's own source: it returns
        # `runtime_status` already flattened to the enum NAME ("COMPLETED", "RUNNING", …) and
        # `serialized_output` as the JSON string the workflow returned. Reading the attributes
        # directly would work — WorkflowState.__getattr__ proxies to the wrapped object — but
        # `runtime_status` is a property that re-maps to a DIFFERENT enum, so the attribute and the
        # dict disagree on their vocabulary. One accessor, one vocabulary.
        return dict(state.to_json())
