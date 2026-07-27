"""Dapr pub/sub subscriber — the durable ingest path for catalog→lineage events (#25).

The catalog publishes OpenLineage events to the Dapr ``pubsub.jetstream`` component; the Dapr sidecar
persists them to NATS JetStream and delivers each to this service over HTTP (a CloudEvent envelope). The
handler ingests into Apache AGE and returns a Dapr status: ``SUCCESS`` (ack), ``RETRY`` (transient — the
sidecar redelivers per the component's ``backOff``/``maxDeliver``, then dead-letter-parks (see below); the
stream retains it and this consumer's ``deliverPolicy: all`` re-sees it on restart), or ``DROP`` (a
malformed payload that redelivery can't fix). Redelivery is safe: the authoritative graph is idempotent
(nodes/edges MERGE on ``run_id``) and the durable events feed dedups on its ``(run_id, event_type,
event_time)`` natural key — only the ``Run.events_count`` is a plain delivery counter (so a redelivery
bumps it). The sidecar owns retry/backoff/trace-propagation as component config, not app code (the
decoupled microservice path — microservices.md). On retry exhaustion the sidecar DEAD-LETTER-PARKS the
delivery on this app's ``dlq.*`` topic (Dapr-native DLQ, default-on via the ``dapr.resiliency.enabled``
chart resiliency); the ``/dlq-event`` route ERROR-logs + acks it — a park-and-alert backstop, NOT
auto-replay (docs/RESILIENCE.md gap #2, fixed 2026-07-12).

Trust model: the topic is an internal, catalog-only channel, so the handler **trusts the verified
``author`` the catalog stamped** — the anti-forgery ``enforce_author`` guard is only for the open HTTP
``/api/v1/lineage`` endpoint, where any producer could lie.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import ValidationError

from lineage.core.metrics import Outcome, record_ingest_duration, record_outcome
from lineage.models import RunEvent
from lineage.services.repository import LineageRepository


log = logging.getLogger(__name__)

# Dapr pub/sub ack statuses (returned to the sidecar so it knows whether to ack / redeliver / drop).
_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}


def feed_fields(event: RunEvent) -> dict[str, Any]:
    """The durable-events-feed columns for one ``RunEvent`` (shared by the HTTP handler + the Dapr
    subscriber so the two ingest paths project identically)."""
    return {
        "run_id": event.run.run_id,
        "event_type": event.event_type,
        "event_time": event.event_time,
        "job": f"{event.job.namespace}/{event.job.name}",
        "author": event.author,
        "inputs": [d.name for d in event.inputs],
        "outputs": [d.name for d in event.outputs],
        "event": event.model_dump(by_alias=True),
    }


async def record_event_best_effort(repository: LineageRepository, event: RunEvent) -> None:
    """Append ``event`` to the durable feed; a feed-write failure must never break ingest (the
    authoritative AGE graph write already succeeded)."""
    try:
        await repository.record_event(**feed_fields(event))
    except Exception as exc:
        log.warning("record_event_failed", extra={"run": event.run.run_id, "error": str(exc)})


async def handle_cloud_event(repository: LineageRepository, body: Any) -> dict[str, str]:
    """Ingest one Dapr-delivered CloudEvent. ``body["data"]`` is the OpenLineage event (Dapr parses it
    since we publish with ``datacontenttype=application/json``). ``body`` is an untrusted external
    envelope, hence ``Any`` + the ``isinstance`` guard. Returns the Dapr ack status."""
    data = body.get("data") if isinstance(body, dict) else None
    try:
        event = RunEvent.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.error("lineage_event_invalid", extra={"error": str(exc)})
        record_outcome(Outcome.DROPPED)
        return _DROP  # malformed — redelivery won't help; drop it (don't poison the subscription)
    started = time.perf_counter()
    try:
        await repository.ingest_event(event)
        await record_event_best_effort(repository, event)
    except Exception as exc:
        log.warning("lineage_ingest_failed", extra={"run": event.run.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED)
        return _RETRY
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED)
    return _SUCCESS
