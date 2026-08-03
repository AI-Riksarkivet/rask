"""services/ingest — pre-bronze acquisition as a platform plane.

Composed through `service_kit.make_service_app` like every other fleet member, so config, error
handlers, middleware, OTel and the Dapr client come from one place (rask-architecture's
entrypoint-over-package contract) rather than being re-assembled here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ingest.api import router as ingest_router
from ingest.runs import InMemoryRunStore


if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the ingest app.

    The run store and the workflow starter hang off `app.state` rather than being imported at module
    scope, so a test can substitute either without a live daprd — which is what keeps A1/A2 fast
    unit tests instead of requiring a cluster to assert a contract that is pure request handling.
    """
    # Populates the SourceAdapter registry by import (I1). A deliberate import-time side effect —
    # the alternative is a hand-maintained list elsewhere, which is exactly the drift the registry
    # exists to prevent.
    from ingest.adapters import register_builtin_sources
    from service_kit import make_service_app

    register_builtin_sources()

    app = make_service_app(title="ingest", routers=[ingest_router])
    app.state.run_store = InMemoryRunStore()
    app.state.workflow_starter = _DaprWorkflowStarter()
    return app


class _DaprWorkflowStarter:
    """Starts `ingest_run` through the Dapr workflow client in the sidecar.

    Imported lazily inside `start` so that constructing the app — which every test does — never
    requires a reachable sidecar.
    """

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        import dapr.ext.workflow as wf

        from ingest.workflow import ingest_run

        client = wf.DaprWorkflowClient()
        client.schedule_new_workflow(workflow=ingest_run, input=payload, instance_id=run_id)
