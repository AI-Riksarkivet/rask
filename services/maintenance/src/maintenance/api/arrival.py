"""The maintenance service's write-event subscription — the PRIMARY trigger.

The sweep discovers by walking every bucket every tick: measured at 87 datasets, one manifest open
each, reporting `fragments_removed: 0, versions_removed: 0` on every pass since 2026-08-16. This lane
replaces that as the primary trigger and leaves the cron as an HOURLY BACKSTOP, which is a correctness
requirement rather than caution — the bus is provably incomplete (ingest, Ray TRAIN and external
OpenLineage producers emit over HTTP only and never reach the topic; the catalog's lineage lane has no
outbox, so a lost trigger is silent), and old-version GC on a table nobody has written since has no
write to react to at all.

Subscribes to `lineage.events.v1`, the one lane every governed writer converges on, with a queue group
so replicas compete rather than each planning the same dataset. The control topic is deliberately not
used: it is a broadcast with no queue group, and it is the notifications plane's people-targeting lane.

Registered only when a work topic is configured — the same condition the cron uses to choose its lane,
so a deployment cannot advertise a subscription for a queue it never publishes to.
"""

from __future__ import annotations

from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool

from maintenance.api.dependencies import DaprClientDep, SettingsDep
from maintenance.core.config import MaintenanceSettings
from maintenance.services.arrival import triggering_write
from maintenance.services.sweep import plan_one
from maintenance.services.work_queue import RETRY, SUCCESS, enqueue_units
from service_kit.draining import retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token


async def handle_arrival(event: dict[str, Any], settings: MaintenanceSettings, dapr: Any) -> dict[str, str]:  # noqa: ANN401 — DaprClient | None
    """Decide whether this write means maintenance, and enqueue one unit if it does.

    ACKS far more often than it enqueues, and every ack is a decision rather than a shrug: a byte-free
    catalog operation, one of maintenance's own completion events (the loop guard), an event carrying
    no physical URI, a trashed or policy-disabled dataset, or a table already at target. None of those
    is retryable, and the hourly backstop re-reaches anything this declines in error.

    RETRIES only when the unit could not be PUBLISHED. That is the one failure redelivery can fix, and
    dropping it silently would make a sidecar outage look like an estate with nothing to do.
    """
    hit = triggering_write(event.get("data", event))
    if hit is None or hit.location is None:
        return {"status": SUCCESS}
    item = await run_in_threadpool(plan_one, hit.location, settings)
    if item is None:
        return {"status": SUCCESS}
    published, _not_queued = await enqueue_units(
        dapr, [item], pubsub=settings.work_pubsub, topic=settings.work_topic, timeout_seconds=settings.publish_timeout_seconds
    )
    return {"status": SUCCESS if published else RETRY}


def register_arrival_route(app: FastAPI, settings: MaintenanceSettings, dapr_app: DaprApp | None = None) -> DaprApp | None:
    """Register the write-event subscription, or nothing when this deployment has no queue.

    Takes an existing :class:`DaprApp` when one was already built for another subscription — a second
    ``DaprApp(app)`` would re-register ``/dapr/subscribe`` and the sidecar would read only one of them.
    """
    if not settings.work_topic:
        return None
    wrapper = dapr_app or DaprApp(app)

    @wrapper.subscribe(
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        route="/maintenance-arrival",
        dead_letter_topic=settings.work_dlq_topic or None,
    )
    async def on_arrival(
        event: dict[str, Any],
        *,
        config: SettingsDep,
        dapr: DaprClientDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
    ) -> dict[str, str]:
        """``event`` is typed ``dict`` so FastAPI parses the CloudEvent body — an ``Any`` param becomes
        a query param and 422s. Authenticated by the Dapr app-api-token: a forged write event could
        otherwise name any URI in any bucket and have this service plan a rewrite of it.

        While draining, ask for REDELIVERY rather than planning — Dapr's delivery does not consult a
        readiness probe, and the plan this would produce could outlive the pod that published it.
        """
        if drain is not None:
            return drain
        return await handle_arrival(event, config, dapr)

    return wrapper


__all__ = ["handle_arrival", "register_arrival_route"]
