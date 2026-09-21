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

from lineage.core.metrics import Door, Outcome, record_ingest_duration, record_outcome
from lineage.models import RunEvent, UnauthoredRunError, UngovernedOutputError
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

    * **DENIED, AND THE ACK DEPENDS ON WHETHER ANYTHING COULD EVER CHANGE THE ANSWER.** A named PERSON
      who lacks a grant keeps the DROP: a tuple can be written and the same event then succeeds on its
      next presentation, so the dead-letter copy is a repairable event held for repair. A run carrying
      NO author cannot be repaired by any tuple, redelivery or restart — `UnauthoredRunError` marks
      that case at the door — so it is ACKED and counted instead.

      **DROP STOPS THE RETRIES, NOT THE PARK, AND THAT IS WHY THE SPLIT EXISTS.** The subscription
      declares a `deadLetterTopic` (`api/dapr.py`), and for Dapr a DROP on such a subscription ROUTES
      the message there. Measured on the live estate 2026-09-15, the sidecar and the app naming the
      same CloudEvent id back to back: daprd logged *"DROP status returned from app while processing
      pub/sub event a4d65ffd-…"*, the app logged `dapr_dead_letter_parked event_id='a4d65ffd-…'`, and
      `POST /lineage-dlq` answered 200. So parking an UNREPAIRABLE event wrote a duplicate of it on
      every roll — the consumer is ephemeral with `deliverPolicy: all`, so each restart re-presents the
      retained stream and re-refuses the same events. One roll produced 49 parks inside two minutes of
      pod start; one run sat in the DLQ twice, five days apart.

      UPSTREAM SETTLES THE ACK, and it is not this estate's preference: dapr/dapr#6282, implemented by
      #7097, has a maintainer state that for a message the app can never accept "SUCCESS is still
      there" — that PR REDEFINED DROP to mean "route to the dead-letter topic". Returning DROP on a
      permanent refusal was asking the broker to KEEP the message.

      THE COUNT IS THE WHOLE TRACE an acked event leaves, which is what makes
      `LineageIngestDiscardingUnrepairable` load-bearing rather than decoration: it reads
      `lance_lineage_outcome="unrepairable"`, and without it the discard would be silent.

      `medallion`'s `transform.py` still parks its deterministic DROPs, and deliberately: its stage
      runners are `deliverPolicy: new`, so a SUCCESS ack there genuinely discards where this lane's
      stream retains and re-presents. [[LH-151]] carries that lane's retention topic.
    * **ANYTHING ELSE -> RETRY.** An unreachable authorization service is an outage, not a verdict, and
      dropping on one would silently delete provenance for the duration of the outage — the failure
      this whole lane exists to prevent. The absent-vs-unreadable rule, at the ack layer.
    """
    data = body.get("data") if isinstance(body, dict) else None
    try:
        event = RunEvent.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.error("lineage_event_invalid", extra={"error": str(exc)})
        record_outcome(Outcome.UNREPAIRABLE, door=Door.SUBSCRIBER)
        # ACKED, not DROPped. A DROP on a subscription carrying a `deadLetterTopic` PARKS, and bytes
        # that do not parse cannot be repaired by a redelivery, a grant or a restart — so parking them
        # writes a dead-letter copy no reader can act on, once per restart, forever. The count is the
        # signal (`Outcome.UNREPAIRABLE`), and the event stays on the stream for its retention.
        return _SUCCESS
    if authorize is not None:
        try:
            await authorize(event)
        except UnauthoredRunError as exc:
            # UNREPAIRABLE, so it is consumed rather than parked — see `UnauthoredRunError`. Measured on
            # the deployed estate 2026-09-18: 37 of 44 refusals in one hour, all one run id, one burst
            # per roll, each appending a NEW dead-letter message about an event the DLQ already held.
            log.warning("lineage_event_unauthored", extra={"run": event.run.run_id, "reason": str(exc)})
            record_outcome(Outcome.UNREPAIRABLE, door=Door.SUBSCRIBER)
            return _SUCCESS
        except UngovernedOutputError as exc:
            # UNREPAIRABLE, so it is consumed rather than parked — a grant needs an OBJECT, and every
            # output this names carries zero tuples. Measured on the deployed estate 2026-09-19: all 7
            # parks in a six-hour window were this, four distinct outputs, none with a tuple and one
            # answering 404 from the catalog. Parked, they came back on every roll forever.
            log.warning("lineage_event_ungoverned_output", extra={"run": event.run.run_id, "reason": str(exc)})
            record_outcome(Outcome.UNREPAIRABLE, door=Door.SUBSCRIBER)
            return _SUCCESS
        except PermissionDeniedError as exc:
            log.warning("lineage_event_unauthorized", extra={"run": event.run.run_id, "reason": str(exc)})
            record_outcome(Outcome.REFUSED, door=Door.SUBSCRIBER)
            return _DROP
        except Exception as exc:
            log.warning("lineage_authz_unavailable", extra={"run": event.run.run_id, "error": str(exc)})
            record_outcome(Outcome.RETRIED, door=Door.SUBSCRIBER)
            return _RETRY
    started = time.perf_counter()
    try:
        # Graph and durable feed in one transaction — a failure here retries BOTH, which is what makes
        # /events a complete projection rather than a subset (see `ingest_event`).
        await repository.ingest_event(event)
    except Exception as exc:
        log.warning("lineage_ingest_failed", extra={"run": event.run.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED, door=Door.SUBSCRIBER)
        return _RETRY
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED, door=Door.SUBSCRIBER)
    return _SUCCESS
