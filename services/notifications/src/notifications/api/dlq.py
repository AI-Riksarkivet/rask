"""The dead-letter parking route.

When a subscription declares a `deadLetterTopic` and the sidecar's retry policy is exhausted, Dapr
publishes the failed delivery here and ACKs the original — instead of the message ceasing to exist
after `maxDeliver`. This route is the parking lot's floor: it counts the park, logs it LOUDLY (ERROR
— operators alert on it) and acks.

**A DLQ handler acks UNCONDITIONALLY.** Returning RETRY from here re-queues a message that has already
failed a full retry schedule, onto the topic whose whole purpose is to be the end of the line — an
infinite loop with no backstop under it. There is deliberately no auto-replay either: the operator
replays from the retained JetStream stream once the cause is fixed.

**The label is THIS app's id**, derived from its own DLQ topic. A shared per-subTopic DLQ once had two
apps subscribed to each other's dead letters, each counting the other's parks; per-app naming is what
makes the counter mean what it says.
"""

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends

from notifications.api.ingest import DAPR_SUCCESS
from notifications.api.metrics import record_dead_letter
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


def register_dlq_route(dapr_app: DaprApp, *, pubsub: str, dlq_topic: str, app_label: str) -> None:
    """Subscribe `dlq_topic` and park deliveries: count + ERROR-log + ack."""

    @dapr_app.subscribe(pubsub=pubsub, topic=dlq_topic, route="/dlq-event")
    async def on_dead_letter(event: dict[str, Any], _: Annotated[None, Depends(require_dapr_token)]) -> dict[str, str]:
        """Park one dead-lettered delivery. `event` is the original CloudEvent Dapr re-published."""
        data = event.get("data")
        run = data.get("run") if isinstance(data, dict) else None
        # Counted BEFORE the log so a permanently stalled notification is a dashboardable and
        # alertable signal rather than only ERROR scrollback.
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
