"""The maintenance work queue: the cron tick PLANS and publishes, a subscription EXECUTES one unit.

The serial sweep maintains the whole estate inside one cron REQUEST, and three of its costs come from
that shape rather than from anything maintenance needs: an overrunning tick is DROPPED by the
single-flight guard, a poison dataset stops everything discovered after it, and the guard is an
``asyncio.Lock`` correct only while ``replicas: 1`` stays hardcoded in the template. One message per
dataset dissolves all three.

**Dapr pub/sub, not a direct NATS client.** ``ingest/queue.py`` is the estate's one sanctioned direct
client (invariant I3) and its own docstring says why: ``max_ack_pending`` backpressure against a
rate-limited external endpoint, batch ``fetch()`` across millions of units, explicit ``nak(delay)``.
Maintenance has none of those needs — tens to hundreds of datasets, no external rate limit — so it
takes the component every other subscriber in the estate takes, and the broker stays a chart value.

**Delivery is at-least-once, and that is safe here rather than merely tolerated.** Compaction and GC
are convergent: a redelivered unit finds the fragments already merged and the versions already
reclaimed, and does nothing. A unit is not a transaction and must never be treated as one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from service_kit.dapr_publish import publish_event


if TYPE_CHECKING:
    from collections.abc import Sequence

    from maintenance.services.sweep import DatasetResult, DatasetWorkItem


log = logging.getLogger(__name__)

#: Dapr's two ack verdicts, as the subscription route must spell them.
SUCCESS = "SUCCESS"
RETRY = "RETRY"


def ack_for(result: DatasetResult) -> str:
    """Should this unit be acked or redelivered? Three outcomes, three answers.

    ``compact_one`` never raises — it captures the per-dataset error so a serial sweep can continue —
    which means the handler cannot infer the verdict from an exception and must read the result. Getting
    this wrong is silent in both directions: always-ack turns every transient S3 failure into a
    dropped dataset, always-retry recirculates an unreadable directory until it dead-letters.

    * ``maintain:`` — compaction or GC genuinely failed, past Lance's own retry. **RETRY**, so the
      broker redelivers on its schedule and parks the unit on the DLQ once ``maxDeliver`` is spent.
      Redelivery does not flood the lineage graph: the FAIL event's run id is deterministic per dataset
      (``lineage_emit.emit_maintenance_failed``), so every redelivery MERGEs onto one node.
    * ``open:`` — the URI would not open: a declared-only directory, a deleted dataset, non-dataset
      noise. **SUCCESS**. There is nothing to retry and nothing is lost; the next tick re-discovers it
      if it becomes real. This is the same class the lineage emit deliberately stays silent about.
    * refused, or a clean pass — **SUCCESS**. A refusal (#64, an unsupported manifest feature flag) is a
      deliberate decline before touching a byte, not a failure, and retrying it would decline again.
    """
    if result.error is not None and result.error.startswith("maintain:"):
        return RETRY
    return SUCCESS


async def enqueue_units(
    publisher: Any,
    items: Sequence[DatasetWorkItem],
    *,
    pubsub: str,
    topic: str,
    timeout_seconds: float,
) -> tuple[int, list[str]]:
    """Publish one message PER UNIT. Returns ``(published, uris that did not make it)``.

    One message per dataset is what creates the failure boundary; batching them would put the whole
    estate back behind a single ack. Each unit crosses as its own serialized document rather than as a
    URI for the worker to re-plan from — a worker cannot recompute the whole-estate protection verdict,
    and re-planning there would silently drop it.

    A publish that fails is COUNTED and returned, never swallowed. That dataset is simply not maintained
    this tick and the next tick re-plans it, which is survivable only while it is visible: swallowed, a
    sidecar outage looks exactly like an estate with nothing to do. One failure does not abandon the
    rest — the remaining units are still worth queueing.
    """
    published = 0
    failed: list[str] = []
    for item in items:
        try:
            await publish_event(
                publisher,
                timeout_seconds=timeout_seconds,
                pubsub_name=pubsub,
                topic_name=topic,
                data=item.model_dump_json(),
                data_content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001 — a failed publish is a skipped dataset, never a failed tick
            log.warning("maintenance_unit_not_queued", extra={"uri": item.uri, "error": str(exc)})
            failed.append(item.uri)
        else:
            published += 1
    return published, failed
