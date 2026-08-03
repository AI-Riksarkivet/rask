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
from ingest.runs import InMemoryRunStore


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# The scheduling call is a blocking gRPC round-trip to the sidecar. Bounded, because A1 is a
# CONTRACT: 202 in under a second. Left unbounded it hangs the request — observed in-cluster at 15s
# and climbing, which turns the estate's flagship async guarantee into a worse experience than the
# synchronous handler it replaced.
SCHEDULE_TIMEOUT_SECONDS = 3.0


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

    app = make_service_app(title="ingest", routers=[health_router, ingest_router], lifespan=_lifespan)
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_starter = _DaprWorkflowStarter()
    app.state.workflow_reader = _DaprWorkflowReader()
    return app


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
