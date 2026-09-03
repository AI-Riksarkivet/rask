"""The maintenance work subscription: ONE dataset per delivery.

The cron tick plans and publishes (``routes.on_cron``); this executes. Registered only when a work
topic is configured, so a local run or the test suite — which take the serial lane — never advertises a
subscription the sidecar would then try to deliver to.

The :class:`DaprApp` wrapper serves ``GET /dapr/subscribe``, which the sidecar reads at startup to learn
this route exists. Authenticated by the Dapr app-api-token, like every other subscription in the estate:
a forged unit could otherwise name any URI in any bucket and have this service rewrite it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from maintenance.api.dependencies import LineageEmitterDep, SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.services.sweep import DatasetWorkItem, emit_sweep_lineage, execute_unit
from maintenance.services.work_queue import SUCCESS, ack_for
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token


async def handle_unit(event: dict[str, Any], settings: MaintenanceSettings, emitter: MaintenanceEmitter) -> dict[str, str]:
    """Execute one work item and decide whether it is done — the testable half of the route.

    A malformed unit is ACKED, not retried. It is the one failure redelivery cannot fix: the message
    will not parse on the tenth attempt either, and retrying it only delays the DLQ while occupying a
    worker. The next tick re-plans that dataset from a planner that produces valid units.

    The lineage emit happens HERE rather than at the tick, because on this lane the tick has no results
    to emit — it returned as soon as the units were published. ``emit_sweep_lineage`` is given the
    single result, which is the same selection logic the serial lane applies to a list.
    """
    try:
        item = DatasetWorkItem.model_validate(event.get("data", event))
    except ValidationError:
        return {"status": SUCCESS}
    result = await run_in_threadpool(execute_unit, item, settings=settings, options=settings.storage_options(), now=datetime.now(UTC))
    await emit_sweep_lineage(emitter, [result], delimiter=settings.delimiter)
    return {"status": ack_for(result)}


def register_work_route(app: FastAPI, settings: MaintenanceSettings) -> DaprApp | None:
    """Register the work subscription, or nothing when this deployment has no queue.

    Returns the :class:`DaprApp` so a caller can hang further subscriptions off the same wrapper; a
    second ``DaprApp(app)`` would re-register ``/dapr/subscribe``.
    """
    if not settings.work_topic:
        return None
    dapr_app = DaprApp(app)

    @dapr_app.subscribe(
        pubsub=settings.work_pubsub,
        topic=settings.work_topic,
        route="/maintenance-work",
        dead_letter_topic=settings.work_dlq_topic or None,
    )
    async def on_unit(
        event: dict[str, Any],
        *,
        config: SettingsDep,
        emitter: LineageEmitterDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes a
        query param and 422s.

        While this replica is draining it asks for REDELIVERY rather than starting work. Dapr's delivery
        does not consult a readiness probe, so without this a pod mid-shutdown would begin a compaction
        it cannot finish — and an interrupted compaction is not merely lost work, it leaves the rewritten
        fragments uncommitted for the next GC to reclaim.
        """
        if drain is not None:
            return drain
        return await handle_unit(event, config, emitter)

    return dapr_app
