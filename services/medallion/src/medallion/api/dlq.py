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

import base64
import binascii
import json
import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, Request

from medallion.core.metrics import record_dead_letter
from service_kit.governed.dapr_auth import require_dapr_token


log = logging.getLogger(__name__)


def register_dlq_route(dapr_app: DaprApp, *, pubsub: str, dlq_topic: str, app_label: str) -> None:
    """Subscribe ``dlq_topic`` and park deliveries: ERROR-log + SUCCESS ack (no retry loop).

    **`rawPayload` IS LOAD-BEARING, and without it a whole class of park was invisible ([[LH-151]]).**
    A body the sidecar cannot read as a CloudEvent fails deserialization on the SOURCE topic, parks,
    and then fails deserialization AGAIN on this one — so the handler never ran, `record_dead_letter`
    never fired, and the DLQ stream and `medallion_dlq_parked_total` silently disagreed by exactly the
    messages nobody could see. Measured on the deployed stage runner 2026-09-19: publishing
    `this-is-not-a-cloudevent-at-all` to `medallion.bronze` moved DLQ 2,522 -> 2,523 while the app
    logged nothing but health probes, and the sidecar logged the same error twice — once per topic.

    `rawPayload` tells Dapr not to attempt that deserialization, so the bytes arrive whatever they are.
    The cost is that a NORMAL park now arrives as the raw CloudEvent rather than a parsed one, which is
    why the envelope is parsed HERE and best-effort: the fields are an enrichment, and failing to read
    them must not re-create the silence this exists to remove.
    """

    @dapr_app.subscribe(pubsub=pubsub, topic=dlq_topic, route="/dlq-event", metadata={"rawPayload": "true"})
    async def on_dead_letter(
        request: Request,
        _: Annotated[None, Depends(require_dapr_token)],
    ) -> dict[str, str]:
        """Park one dead-lettered delivery, readable or not."""
        body = await request.body()
        envelope = _envelope(body)
        # Count it (bounded by app_label) BEFORE the log so a permanently-stalled cascade item is a
        # dashboardable + alertable signal, not only ERROR scrollback — the cascade twin of the lineage
        # DLQ's record_outcome(DEAD_LETTERED). (prod-readiness P1)
        record_dead_letter(app_label)
        data = envelope.get("data") if envelope else None
        log.error(
            "dapr_dead_letter_parked",
            extra={
                "app": app_label,
                "dlq_topic": dlq_topic,
                "event_id": envelope.get("id") if envelope else None,
                "source_topic": envelope.get("topic") if envelope else None,
                "token": data.get("token") if isinstance(data, dict) else None,
                # THE FIELD THAT MAKES THE INVISIBLE CLASS VISIBLE. True means the bytes are not a
                # CloudEvent at all, so no `event_id` exists to join on and the only handle an operator
                # has is this log plus the DLQ stream offset.
                "undeserializable": envelope is None,
                "bytes": len(body),
            },
        )
        return {"status": "SUCCESS"}


def _envelope(body: bytes) -> dict[str, Any] | None:
    """The parked CloudEvent, or ``None`` when the bytes are not one.

    Dapr wraps a rawPayload delivery in its own envelope carrying the original bytes as `data` or
    `data_base64`, so the parked CloudEvent is one level in. BEST-EFFORT by design: every field read
    from it is an enrichment on a log line that must be emitted either way.
    """
    try:
        outer = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(outer, dict):
        return None
    # THE TWO SHAPES ARE TOLD APART BY `data`'s TYPE, not by guessing. A wrapper carries the inner
    # CloudEvent as TEXT (`data` a str, or `data_base64`); a CloudEvent carries its own payload as a
    # dict. Treating a dict `data` as a wrapper returns the PAYLOAD as the envelope — every field then
    # reads empty and the park logs a null id an operator cannot tell from a real one.
    encoded = outer.get("data") if isinstance(outer.get("data"), str) else None
    if encoded is None and isinstance(outer.get("data_base64"), str):
        try:
            encoded = base64.b64decode(outer["data_base64"]).decode()
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
    if encoded is not None:
        try:
            inner = json.loads(encoded)
        except (ValueError, UnicodeDecodeError):
            return None
        return inner if isinstance(inner, dict) and "id" in inner else None
    return outer if "id" in outer else None
