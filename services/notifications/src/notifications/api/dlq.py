"""The dead-letter parking route.

When a subscription declares a `deadLetterTopic` and the sidecar's retry policy is exhausted, Dapr
publishes the failed delivery here and ACKs the original — instead of the message ceasing to exist
after `maxDeliver`. This route is the parking lot's floor: it counts the park, logs it LOUDLY (ERROR
— operators alert on it) and acks.

**A control event is KEPT, and re-driven** (`notifications.api.control_parks`, LH-331). A run notification
that dead-letters still reaches its person through the reconcile cron's walk of lineage's durable feed; a
governance notification has no such feed, so this route parks it and every reconcile tick offers it again.

**Otherwise a DLQ handler acks.** Returning RETRY from here re-queues a message that has already failed a
full retry schedule, onto the topic whose whole purpose is to be the end of the line. The one RETRY is a
control event the park could not keep: acking it would lose the only copy, and the dead-letter
subscription's own redelivery bounds the retry.

**The label is THIS app's id**, derived from its own DLQ topic. A shared per-subTopic DLQ once had two
apps subscribed to each other's dead letters, each counting the other's parks; per-app naming is what
makes the counter mean what it says.
"""

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, Request

from notifications.api.control_parks import ControlParksUnavailable, control_event_id, get_control_parks, park
from notifications.api.ingest import DAPR_RETRY, DAPR_SUCCESS
from notifications.api.metrics import record_dead_letter
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


def register_dlq_route(dapr_app: DaprApp, *, pubsub: str, dlq_topic: str, app_label: str) -> None:
    """Subscribe `dlq_topic` and park deliveries: count + ERROR-log, keep a control event for its re-drive, ack."""

    @dapr_app.subscribe(pubsub=pubsub, topic=dlq_topic, route="/dlq-event")
    async def on_dead_letter(event: dict[str, Any], request: Request, _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
        """Park one dead-lettered delivery. `event` is the original CloudEvent Dapr re-published."""
        data = event.get("data")
        run = data.get("run") if isinstance(data, dict) else None
        event_id = control_event_id(data)
        if event_id is not None and isinstance(data, dict):
            try:
                if not await park(get_control_parks(request), event_id=event_id, data=data):
                    # A redelivery of a dead letter already kept: counted and logged when it was first kept.
                    return DAPR_SUCCESS
            except ControlParksUnavailable:
                log.exception("control_park_unavailable", extra={"event_id": event_id})
                return DAPR_RETRY
        # Counted BEFORE the log so a permanently stalled notification is a dashboardable and
        # alertable signal rather than only ERROR scrollback; a control event once it is kept, so its
        # redeliveries do not count it again.
        record_dead_letter(app_label)
        log.error(
            "dapr_dead_letter_parked",
            extra={
                "app": app_label,
                "dlq_topic": dlq_topic,
                "event_id": event.get("id"),
                "source_topic": event.get("topic"),
                # The run this notification was about — the one field that lets an operator find the
                # cause. Read defensively: a parked payload is by definition one that something
                # already failed to handle, and it may be malformed.
                "run_id": run.get("runId") if isinstance(run, dict) else None,
            },
        )
        return DAPR_SUCCESS
