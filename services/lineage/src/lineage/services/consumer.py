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

Trust model: the topic is an internal channel and the sidecar's shared app-api-token is what opens this
door, so the door authenticates a TRANSPORT, never a producer. The author on the payload is therefore a
CLAIM, and the ``authorize`` hook below is what makes the claim cost something: the stamped subject must
hold the rung the event's operation demands on every dataset the event says it wrote
(``fga_deps.enforce_bus_authz``). A forged stamp then buys nothing a forger did not already have.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from lance_namespace import PermissionDeniedError
from pydantic import ValidationError

from lineage.core.metrics import Outcome, record_ingest_duration, record_outcome
from lineage.models import RunEvent
from lineage.services.repository import LineageRepository


log = logging.getLogger(__name__)

# Dapr pub/sub ack statuses (returned to the sidecar so it knows whether to ack / redeliver / drop).
_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}


async def handle_cloud_event(repository: LineageRepository, body: Any, authorize: Callable[[RunEvent], Awaitable[None]] | None = None) -> dict[str, str]:
    """Ingest one Dapr-delivered CloudEvent. ``body["data"]`` is the OpenLineage event (Dapr parses it
    since we publish with ``datacontenttype=application/json``). ``body`` is an untrusted external
    envelope, hence ``Any`` + the ``isinstance`` guard. Returns the Dapr ack status.

    ``authorize`` runs after the parse and before any write. It is injected rather than imported so this
    module stays free of FGA — the door owns the policy, this owns the ack contract — and it splits the
    two failure kinds the way the sidecar needs them:

    * **DENIED -> DROP.** Redelivery cannot grant a permission, so retrying a refused event only burns
      the delivery budget and then parks a permanent refusal on the dead-letter topic as if it were an
      outage. Same reasoning the malformed branch above already uses.
    * **ANYTHING ELSE -> RETRY.** An unreachable authorization service is an outage, not a verdict, and
      dropping on one would silently delete provenance for the duration of the outage — the failure
      this whole lane exists to prevent. The absent-vs-unreadable rule, at the ack layer.
    """
    data = body.get("data") if isinstance(body, dict) else None
    try:
        event = RunEvent.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.error("lineage_event_invalid", extra={"error": str(exc)})
        record_outcome(Outcome.DROPPED)
        return _DROP  # malformed — redelivery won't help; drop it (don't poison the subscription)
    if authorize is not None:
        try:
            await authorize(event)
        except PermissionDeniedError as exc:
            log.warning("lineage_event_unauthorized", extra={"run": event.run.run_id, "reason": str(exc)})
            record_outcome(Outcome.REFUSED)
            return _DROP
        except Exception as exc:
            log.warning("lineage_authz_unavailable", extra={"run": event.run.run_id, "error": str(exc)})
            record_outcome(Outcome.RETRIED)
            return _RETRY
    started = time.perf_counter()
    try:
        # Graph and durable feed in one transaction — a failure here retries BOTH, which is what makes
        # /events a complete projection rather than a subset (see `ingest_event`).
        await repository.ingest_event(event)
    except Exception as exc:
        log.warning("lineage_ingest_failed", extra={"run": event.run.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED)
        return _RETRY
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED)
    return _SUCCESS
