"""Dapr pub/sub subscriber — the durable ingest path for catalog→lineage events (#25).

The catalog publishes OpenLineage events to the Dapr ``pubsub.jetstream`` component; the Dapr sidecar
persists them to NATS JetStream and delivers each to this service over HTTP (a CloudEvent envelope). The
handler records it in the commit feed and the edge list and returns a Dapr status: ``SUCCESS`` (ack), ``RETRY`` (transient — the
sidecar redelivers per the component's ``backOff``/``maxDeliver``, then dead-letter-parks (see below); the
stream retains it and this consumer's ``deliverPolicy: all`` re-sees it on restart), or ``DROP`` (a
malformed payload that redelivery can't fix). Redelivery is safe: every feed row and edge is written under
its key with an insert-only merge, so a redelivered event lands nothing new (``ProvenanceStore.record``). The sidecar owns retry/backoff/trace-propagation as component config, not app code (the
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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from lance_namespace import PermissionDeniedError
from pydantic import ValidationError

from lineage.core.metrics import Door, Outcome, record_ingest_duration, record_outcome, refusal_outcome
from lineage.models import DatasetEvent, ForeignRunError, RunEvent, parse_event
from lineage.services.provenance import ProvenanceStore


log = logging.getLogger(__name__)

# Dapr pub/sub ack statuses (returned to the sidecar so it knows whether to ack / redeliver / drop).
_SUCCESS = {"status": "SUCCESS"}
#: The ack that asks the sidecar to deliver again: an outage, never a verdict.
RETRY: Final = "RETRY"
_RETRY = {"status": RETRY}
_DROP = {"status": "DROP"}


async def handle_cloud_event(
    provenance: ProvenanceStore,
    body: Any,
    authorize: Callable[[RunEvent | DatasetEvent, Mapping[str, Any]], Awaitable[None]] | None = None,
    *,
    door: Door,
) -> dict[str, str]:
    """Ingest one Dapr-delivered CloudEvent. ``body["data"]`` is the OpenLineage event, which the envelope its
    publisher built carries as JSON (`lakehouse.cloud_events`). ``body`` is an untrusted external
    envelope, hence ``Any`` + the ``isinstance`` guard. Returns the Dapr ack status.

    ``door`` is the ingress the delivery arrived through, with no default: every outcome below is counted under
    it, and the parking route re-presents a parked delivery here, where a hard-coded subscriber label would count
    one refusal as an arrival and then again as a park.

    ``authorize`` runs after the parse and before any write. It is injected rather than imported so this
    module stays free of FGA — the door owns the policy, this owns the ack contract — and it splits the
    two failure kinds the way the sidecar needs them:

    * **AN EVENT NO LISTED SIGNER'S KEY VERIFIES IS ACKED** ([[LH-064]], `UnverifiedEventError`). Unsigned, forged
      or signed by an identity the chart does not list: no redelivery can add a signature to published bytes, so a
      DROP would only park a copy per restart. It is counted (`Outcome.SIGNATURE_REFUSED`, and the reason on
      `lineage.signature.refused`) and never recorded. A key list that cannot be read is the other kind, an
      outage, and falls to the RETRY arm below: refusing on it would destroy honest events during a store blip.

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
      `POST /lineage-dlq` answered 200. So parking an UNREPAIRABLE event writes a duplicate of it on
      every replay of the retained stream, which re-refuses the same events: one replay produced 49
      parks inside two minutes; one run sat in the DLQ twice, five days apart.

      UPSTREAM SETTLES THE ACK, and it is not this estate's preference: dapr/dapr#6282, implemented by
      #7097, has a maintainer state that for a message the app can never accept "SUCCESS is still
      there" — that PR REDEFINED DROP to mean "route to the dead-letter topic". Returning DROP on a
      permanent refusal was asking the broker to KEEP the message.

      THE COUNT IS THE WHOLE TRACE an acked event leaves, which is what makes
      `LineageIngestDiscardingUnrepairable` load-bearing rather than decoration: it reads
      `lance_lineage_outcome="unrepairable"`, and without it the discard would be silent.
    * **ANYTHING ELSE -> RETRY.** An unreachable authorization service is an outage, not a verdict, and
      dropping on one would silently delete provenance for the duration of the outage — the failure
      this whole lane exists to prevent. The absent-vs-unreadable rule, at the ack layer.
    """
    data = body.get("data") if isinstance(body, dict) else None
    try:
        event: RunEvent | DatasetEvent = parse_event(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.error("lineage_event_invalid", extra={"error": str(exc)})
        record_outcome(Outcome.UNREPAIRABLE, door=door)
        # ACKED, not DROPped. A DROP on a subscription carrying a `deadLetterTopic` PARKS, and bytes
        # that do not parse cannot be repaired by a redelivery, a grant or a restart — so parking them
        # writes a dead-letter copy no reader can act on, once per restart, forever. The count is the
        # signal (`Outcome.UNREPAIRABLE`), and the event stays on the stream for its retention.
        return _SUCCESS
    if authorize is not None:
        try:
            # THE ARRIVED DICT RIDES ALONG, not just the parsed event. The door's signature check covers
            # the bytes the producer signed, and a model is a reconstruction of them — it grows every
            # field the schema defaults. `data` is what Dapr delivered; the parse above is for everything
            # else.
            await authorize(event, data if isinstance(data, dict) else {})
        except PermissionDeniedError as exc:
            # ONE VERDICT (:func:`refusal_outcome`), the one every door that records an event counts by. Only a named
            # person lacking a grant keeps the DROP that parks the delivery for repair; every other refusal is final, so
            # it is acked and its count is the whole trace it leaves (each refusal type says why it is final).
            outcome = refusal_outcome(exc)
            log.warning("lineage_event_refused", extra={"run": event.run_id, "outcome": outcome, "reason": str(exc)})
            record_outcome(outcome, door=door)
            return _DROP if outcome is Outcome.REFUSED else _SUCCESS
        except Exception as exc:
            log.warning("lineage_authz_unavailable", extra={"run": event.run_id, "error": str(exc)})
            record_outcome(Outcome.RETRIED, door=door)
            return _RETRY
    started = time.perf_counter()
    try:
        # The edges, then the feed row, each keyed: a failure here retries the delivery, and the retry lands
        # what is missing and nothing twice (see `ProvenanceStore.record`).
        await provenance.record(event)
    except ForeignRunError as exc:
        # The commit's own refusal of a run another author bound first, among events delivered together: as
        # unrepairable as the door's.
        log.warning("lineage_event_foreign_run", extra={"run": event.run_id, "reason": str(exc)})
        record_outcome(Outcome.UNREPAIRABLE, door=door)
        return _SUCCESS
    except Exception as exc:
        log.warning("lineage_ingest_failed", extra={"run": event.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED, door=door)
        return _RETRY
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED, door=door)
    return _SUCCESS
