"""The handling of one event delivered from the bus: the durable ingest path for catalog→lineage events (#25).

The catalog publishes OpenLineage events to NATS JetStream, which keeps them, and this service pulls each from its
own consumer on its own connection (`lineage.api.ingest_lane`, `lakehouse.events.bus`). The handling records the event in
the commit feed and the edge list and answers the lane what became of the delivery (`lakehouse.events.bus_delivery.Verdict`):
*taken* (recorded, or refused for good), *again* (a dependency did not answer; the lane delivers it again after its
delay, and parks it on its last delivery), or *park* (a refusal a grant can repair, kept for one). Redelivery is safe:
every feed row and edge is written under its key with an insert-only merge, so a redelivered event lands nothing new
(``ProvenanceStore.record``).

Trust model: the topic is an internal channel and the bus's permission table is what lets a producer publish on it, so
the lane authenticates a TRANSPORT, never a producer. The author on the payload is therefore a CLAIM, and the
``authorize`` hook below is what makes the claim cost something: the stamped subject must hold the rung the event's
operation demands on every dataset the event says it wrote (``fga_deps.enforce_bus_authz``). A forged stamp then buys
nothing a forger did not already have.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import urllib3
from lance_namespace import PermissionDeniedError, ServiceUnavailableError
from pydantic import ValidationError

from lakehouse.events.bus_delivery import Verdict
from lakehouse.problems import domain_errors
from lineage.core.metrics import Door, Outcome, record_ingest_duration, record_outcome, refusal_outcome
from lineage.core.refusals import ForeignRunError
from lineage.models import DatasetEvent, RunEvent, parse_event
from lineage.services.provenance import ProvenanceStore


log = logging.getLogger(__name__)

#: What a recording meets that its delivery is asked for again over, counted ``retried``: every answer the catalog gave
#: the feed's read or write, translated into the ``DomainError`` family (`catalog_reader.translate_catalog_errors`: a
#: 5xx or 429, a conflict another writer won, a row it refused, which the writer's halving leaves to the one event that
#: carries it), this service's token unreadable (`machine_identity.read_identity_token`, a ``DomainError`` too), and a
#: catalog the transport did not reach (urllib3's ``HTTPError``). Anything else is a defect and reaches the delivery
#: seam, which asks for the delivery again with its traceback and parks it on its last (`lakehouse.events.bus_delivery`).
FEED_FAILURES: Final = (domain_errors.DomainError, urllib3.exceptions.HTTPError)
#: The outages an authorization meets, each asked for again and counted ``retried``: OpenFGA, the catalog's decision
#: doors or the signers' published keys not answering (``ServiceUnavailableError``: `fga_resilience.guarded`,
#: `decider`, `signature.verify_arrived_event`), and the feed read a replay's check makes (:data:`FEED_FAILURES`).
AUTHORIZATION_OUTAGES: Final = (ServiceUnavailableError, *FEED_FAILURES)


async def handle_cloud_event(
    provenance: ProvenanceStore,
    body: Any,
    authorize: Callable[[RunEvent | DatasetEvent, Mapping[str, Any]], Awaitable[frozenset[str]]],
    *,
    door: Door,
) -> Verdict:
    """Ingest one delivered CloudEvent. ``body["data"]`` is the OpenLineage event, which the envelope its publisher
    built carries as JSON (`lakehouse.events.cloud_events`). ``body`` is an untrusted external envelope, hence ``Any`` + the
    ``isinstance`` guard. Returns what the lane does with the delivery.

    ``door`` is the ingress the delivery arrived through, with no default: every outcome below is counted under it,
    and the estate admin's replay re-presents a parked delivery here, where a hard-coded subscriber label would count
    one refusal as an arrival and then again as a park.

    ``authorize`` runs after the parse and before any write, and answers the tables the event's datasets alias, which
    its feed row is scoped by (`fga_deps.enforce_bus_authz`). It is injected rather than imported so this module stays
    free of FGA — the door owns the policy, this owns what a delivery is answered. It has no default, as ``door`` has
    none: a call site that named no gate would record every event unjudged. It splits the failure kinds the way the
    lane needs them:

    * **AN EVENT NO LISTED SIGNER'S KEY VERIFIES IS TAKEN** ([[LH-064]], `UnverifiedEventError`). Unsigned, forged or
      signed by an identity the chart does not list: no redelivery can add a signature to published bytes, so a park
      would only keep a copy nothing can repair. It is counted (`Outcome.SIGNATURE_REFUSED`, and the reason on
      `lineage.signature.refused`) and never recorded. A key list that cannot be read is the other kind, an outage,
      and falls to the *again* arm below: refusing on it would destroy honest events during a store blip.

    * **DENIED, AND THE ANSWER DEPENDS ON WHETHER ANYTHING COULD EVER CHANGE IT.** A named PERSON who lacks a grant is
      parked: a tuple can be written and the same event then succeeds on its next presentation, so the kept copy is a
      repairable event held for repair (`ingest_lane.park`, the provenance namespace's ``refusals`` table). That is the
      rule at every door that cannot answer its producer: the relay parks a staged event refused this way through the
      same park, and the estate admin's replay re-presents either here. A run carrying
      NO author cannot be repaired by any tuple, redelivery or restart — `UnauthoredRunError` marks that case at the
      door — so it is TAKEN and counted instead: parking it would keep a copy no reader can act on, and log and count
      it as a loss again on every replay of the retained stream, which re-refuses the same events (measured
      2026-09-15: one replay produced 49 parks inside two minutes; one run was parked twice, five days apart).

      THE COUNT IS THE WHOLE TRACE a taken refusal leaves, which is what makes
      `LineageIngestDiscardingUnrepairable` load-bearing rather than decoration: it reads
      `lance_lineage_outcome="unrepairable"`, and without it the discard would be silent.
    * **ANYTHING ELSE -> AGAIN.** An unreachable authorization service is an outage, not a verdict, and ending the
      delivery on one would silently delete provenance for the duration of the outage — the failure this whole lane
      exists to prevent. The absent-vs-unreadable rule, at the delivery.
    """
    data = body.get("data") if isinstance(body, dict) else None
    try:
        event: RunEvent | DatasetEvent = parse_event(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.error("lineage_event_invalid", extra={"error": str(exc)})
        record_outcome(Outcome.UNREPAIRABLE, door=door)
        # TAKEN, not parked. Bytes that do not parse cannot be repaired by a redelivery, a grant or a restart — so
        # parking them would keep a copy no reader can act on. The count is the signal (`Outcome.UNREPAIRABLE`), and
        # the event stays on the stream for its retention.
        return Verdict.TAKEN
    try:
        # THE ARRIVED DICT RIDES ALONG, not just the parsed event. The door's signature check covers
        # the bytes the producer signed, and a model is a reconstruction of them — it grows every
        # field the schema defaults. `data` is what the bus delivered; the parse above is for everything
        # else.
        aliased = await authorize(event, data if isinstance(data, dict) else {})
    except PermissionDeniedError as exc:
        # ONE VERDICT (:func:`refusal_outcome`), the one every door that records an event counts by. Only a named
        # person lacking a grant is parked for repair; every other refusal is final, so it is taken and its count
        # is the whole trace it leaves (each refusal type says why it is final).
        outcome = refusal_outcome(exc)
        log.warning("lineage_event_refused", extra={"run": event.run_id, "outcome": outcome, "reason": str(exc)})
        record_outcome(outcome, door=door)
        return Verdict.PARK if outcome is Outcome.REFUSED else Verdict.TAKEN
    except AUTHORIZATION_OUTAGES as exc:
        log.warning("lineage_authz_unavailable", extra={"run": event.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED, door=door)
        return Verdict.AGAIN
    started = time.perf_counter()
    try:
        # The edges, then the feed row, each keyed: a failure here asks for the delivery again, and the next one lands
        # what is missing and nothing twice (see `ProvenanceStore.record`).
        await provenance.record(event, aliased=aliased)
    except ForeignRunError as exc:
        # The commit's own refusal of a run another author bound first, among events delivered together: as
        # unrepairable as the door's.
        log.warning("lineage_event_foreign_run", extra={"run": event.run_id, "reason": str(exc)})
        record_outcome(Outcome.UNREPAIRABLE, door=door)
        return Verdict.TAKEN
    except FEED_FAILURES as exc:
        log.warning("lineage_ingest_failed", extra={"run": event.run_id, "error": str(exc)})
        record_outcome(Outcome.RETRIED, door=door)
        return Verdict.AGAIN
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED, door=door)
    return Verdict.TAKEN
