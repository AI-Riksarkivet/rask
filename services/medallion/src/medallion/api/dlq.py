"""The medallion dead-letter parking route (Dapr-native DLQ — docs/RESILIENCE.md gap #2).

When a subscription declares a ``deadLetterTopic`` and the sidecar's Resiliency retry policy is
exhausted, Dapr publishes the failed delivery here and ACKs the original — instead of the message
silently ceasing to exist after ``maxDeliver``. This route is the parking lot's floor: it logs the
parked message LOUDLY (ERROR — operators alert on it) and acks. Deliberately no auto-requeue: the
message already failed a full retry schedule, so re-firing it blind would loop the cascade; the
operator replays from the retained JetStream stream (or re-triggers the stage) after fixing the
cause. Registered only when the app's DLQ topic is configured, so default deployments are unchanged.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from common.dapr_auth import require_dapr_token
from dapr.ext.fastapi import DaprApp
from fastapi import Depends

from medallion.core.metrics import record_dead_letter


log = logging.getLogger(__name__)


def register_dlq_route(dapr_app: DaprApp, *, pubsub: str, dlq_topic: str, app_label: str) -> None:
    """Subscribe ``dlq_topic`` and park deliveries: ERROR-log + SUCCESS ack (no retry loop)."""

    @dapr_app.subscribe(pubsub=pubsub, topic=dlq_topic, route="/dlq-event")
    async def on_dead_letter(
        event: dict[str, Any],
        _: Annotated[None, Depends(require_dapr_token)],
    ) -> dict[str, str]:
        """Park one dead-lettered delivery. ``event`` is the original CloudEvent Dapr re-published."""
        data = event.get("data") if isinstance(event, dict) else None
        # Count it (bounded by app_label) BEFORE the log so a permanently-stalled cascade item is a
        # dashboardable + alertable signal, not only ERROR scrollback — the cascade twin of the lineage
        # DLQ's record_outcome(DEAD_LETTERED). (prod-readiness P1)
        record_dead_letter(app_label)
        log.error(
            "dapr_dead_letter_parked",
            extra={
                "app": app_label,
                "dlq_topic": dlq_topic,
                "event_id": event.get("id") if isinstance(event, dict) else None,
                "source_topic": event.get("topic") if isinstance(event, dict) else None,
                "token": data.get("token") if isinstance(data, dict) else None,
            },
        )
        return {"status": "SUCCESS"}
