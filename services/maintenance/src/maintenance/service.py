"""Table maintenance's web process: a FastAPI app serving the write-event lane (ADR 0198).

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6, ADR 0162). Its work is tasks on a NATS JetStream
work queue, run by the worker, a process of its own in pods of its own (:mod:`maintenance.worker`). This process runs no
task. With ``MAINTENANCE_ARRIVAL_ENABLED`` it serves ``/maintenance-arrival``, which queues a written table's pass, and
holds the queue's connection for those publishes alone. Run: ``uvicorn maintenance.service:app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext

from fastapi import FastAPI

from lakehouse.draining import arm_drain_on_sigterm
from lakehouse.governed.dapr_auth import assert_app_token_configured
from lakehouse.lance_app import build_lance_service_app
from lakehouse.obs import configure_app_logging
from lakehouse.supervision import Supervisor
from maintenance.api.arrival import register_arrival_route
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.runtime import reported_health, reported_readiness, standing
from maintenance.services.queue import TaskQueue
from maintenance.services.tasks import connected_queue


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.startup_complete = False
    app.state.shutting_down = False
    # ARMED ON THE SIGNAL, not on lifespan unwind: the `finally` below runs after uvicorn has stopped serving, so
    # `retry_when_draining` on the arrival route could never refuse a delivery.
    _disarm_drain = arm_drain_on_sigterm(app)
    try:
        settings = get_settings()
        # Fail closed if behind a Dapr sidecar but the app-token is unset: the write-event route would otherwise be an
        # open forged-commit path (symmetric with the lineage service). No-op in dev (dapr_enabled off).
        assert_app_token_configured(dapr_enabled=settings.dapr_enabled)
        # Every background loop of this process is supervised: one that ends unasked ends the process (ADR 0203).
        async with Supervisor() as loops, standing(app, settings, loops), _lanes_queue(settings) as task_queue:
            app.state.task_queue = task_queue
            # Reported and never gating: the lane's deliveries come from the sidecar, which no readiness answer turns away.
            app.state.health = reported_health(settings, task_queue)
            app.state.health.watch(loops)
            app.state.startup_complete = True
            try:
                yield
            finally:
                app.state.shutting_down = True
                await loops.stop()
    finally:
        _disarm_drain()


def _lanes_queue(settings: MaintenanceSettings) -> AbstractAsyncContextManager[TaskQueue | None]:
    """The queue's connection, held only while the write-event lane is served: nothing else here publishes to it, and a
    process that serves no lane reads no bus credential."""
    return connected_queue(settings) if settings.arrival_enabled and settings.lineage_topic else nullcontext()


# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, every layer a request passes, and the probes
# — see `lakehouse.lance_app` for what each is for and what a copy of it got wrong.
app = build_lance_service_app(
    title="Lance table maintenance",
    docs_enabled=get_settings().docs_enabled,
    audit_enabled=get_settings().audit_enabled,
    lifespan=lifespan,
    log=log,
    ready_check=reported_readiness,
    max_body_bytes=get_settings().max_body_bytes,
    request_deadline_seconds=get_settings().request_deadline_seconds,
)

# The write-event subscription (POST /maintenance-arrival), registered only when the lane is enabled.
register_arrival_route(app, get_settings())
