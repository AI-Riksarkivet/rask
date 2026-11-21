"""Lineage's ingest lane: every event on `lineage.events.v1`, pulled on this service's own connection to the bus.

The chart's stream Job declares the durable consumer (:data:`CONSUMER` on :data:`STREAM`); lineage binds to it,
creates nothing, and its replicas share it, so each event is delivered to one of them. `lakehouse.events.bus` pulls and
settles; this module is what lineage does with one delivery, and what it keeps of one it ends without recording.

A DELIVERY IS HANDLED BY THE BUS DOOR'S OWN GATE (`consumer.handle_cloud_event` under `fga_deps.enforce_bus_authz`): the
signature over the bytes that arrived, and what the stamped author may record.

A PARKED DELIVERY IS KEPT WHERE THE ESTATE ADMIN CAN RE-PRESENT IT ([[LH-148]]). The lane parks a delivery a grant
could repair, and one its last delivery could not record. The stream drops a message past its retention and nothing
reads an ended one back, so the provenance namespace's ``refusals`` table holds the delivery instead, keyed by its
CloudEvent id, or by the feed row it would write when its envelope carries none (a park of the same event again lands
on its row), until ``POST /admin/dlq/parked/{run_id}/replay`` records it. The relay parks a staged event a grant could
repair the same way (:func:`park`), under the id the bus copy of it carries, so one event reaching lineage by both
routes is one kept row. A park the feed already holds the row of is kept nowhere: the
event is recorded, and what was ended is its redelivery.

A PARK MUST NOT FAIL FOR WHAT ITS DELIVERY CARRIES. The bus delivers a message until this lane ends it, and a park that
fails is asked for again, so one that failed for the delivery's own bytes would fail on every delivery after it, and
nothing else ends the message. A delivery is its publisher's bytes, parked before any signature admits it, so what the
park takes from it for a row or a log line is plain text or nothing (:func:`_plain`).
Measured 2026-11-14 on the locked pyarrow and opentelemetry-sdk: an id holding a lone surrogate (``"\\ud800"`` in the
body) raises `UnicodeEncodeError` in the table's writer, and the log handler the estate attaches (`LoggingHandler`)
renders each attribute of a record by recursion and raises what that raises.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

from lakehouse.events.bus_delivery import Delivery, Lane, Verdict
from lineage.api.fga_deps import enforce_bus_authz
from lineage.core.config import LineageDelivery, LineageSettings
from lineage.core.metrics import Door, Outcome, record_outcome
from lineage.models import DatasetEvent, RunEvent, author_sub_from_payload, run_id_from_payload
from lineage.services.consumer import handle_cloud_event
from lineage.services.provenance import PARKED_KEY_PREFIX, ProvenanceStore, delivery_feed_key


if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI


log = logging.getLogger(__name__)

#: The stream lineage's events are kept on, and lineage's durable consumer on it, as the chart's stream Job declares
#: them (`chart/templates/nats-stream-job.yaml`).
STREAM: Final = "LINEAGE"
CONSUMER: Final = "lineage-ingest"
#: The most of a delivery's own text a kept row's key, its run and author, and a log line carry.
_MAX_PLAIN_CHARS: Final = 512


def _plain(value: object) -> str | None:
    """``value`` when a table and a log line carry it as it stands: a string of 1 to `_MAX_PLAIN_CHARS` characters,
    each with a UTF-8 form. Anything else is None."""
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_PLAIN_CHARS:
        return None
    try:
        value.encode()
    except UnicodeEncodeError:
        return None
    return value


def lane(settings: LineageDelivery) -> Lane:
    """The consumer lineage pulls, and the numbers the chart gives its deliveries."""
    return Lane(
        stream=STREAM,
        consumer=CONSUMER,
        attempts=settings.delivery_attempts,
        retry_seconds=settings.delivery_retry_seconds,
        ack_wait_seconds=settings.delivery_ack_wait_seconds,
        in_flight=settings.deliveries_in_flight,
    )


class IngestLane:
    """What lineage does with each delivery of its consumer, on the feed and the gate ``app`` holds."""

    def __init__(self, app: FastAPI, settings: LineageSettings) -> None:
        self._app = app
        self._settings = settings

    async def handle(self, delivery: Delivery) -> Verdict:
        """Record the event ``delivery`` carries, or answer why not (`consumer.handle_cloud_event`)."""
        return await handle_cloud_event(self._provenance, delivery.envelope(), self._authorize, door=Door.SUBSCRIBER)

    async def park(self, delivery: Delivery, reason: str) -> None:
        """Keep a delivery the lane ends without recording (:func:`park`)."""
        await park(self._provenance, delivery.envelope(), reason)

    @property
    def _provenance(self) -> ProvenanceStore:
        return self._app.state.provenance

    async def _authorize(self, parsed: RunEvent | DatasetEvent, arrived: Mapping[str, Any]) -> frozenset[str]:
        return await enforce_bus_authz(parsed, self._app, self._settings, arrived=arrived)


async def park(provenance: ProvenanceStore, envelope: Mapping[str, Any] | None, reason: str) -> None:
    """Keep ``envelope``, an event lineage ends without recording, unless the feed already holds its row, and count which.

    THE ONE PARK, for both ways an event reaches lineage without its producer waiting on the answer: the ingest lane's
    delivery, and the relay's drain of a staged event a grant could repair (`relay._drain_outbox`), which hands it the
    envelope the bus would have delivered. Either is listed and replayed at the same ``/admin/dlq/parked`` doors.

    SEVERITY IS THE ANSWER, not the event. A replay of the retained stream parks again whatever still fails, so an
    unconditional ERROR per park would count replays rather than losses, and bury the real ones among them
    (measured on the live estate 2026-09-13: 8,515 parked deliveries of `lineage.events.v1` against a stream whose
    last sequence was 5,860, and 17 of 21 distinct parked run ids already recorded).

    Raises:
        Exception: The feed could not be asked, or the ``refusals`` table did not take the event: whatever the catalog's
            door raised. The lane asks for the delivery again, and the relay leaves the staged object for its next tick.
    """
    payload = envelope.get("data") if envelope is not None else None
    event_id = _plain(envelope.get("id")) if envelope is not None else None
    run_id = _plain(run_id_from_payload(payload))
    author = _plain(author_sub_from_payload(payload))
    recorded = envelope is not None and await _feed_holds(provenance, envelope)
    if envelope is not None and not recorded:
        await _keep(provenance, envelope, event_id=event_id, run_id=run_id, author=author, reason=reason)
    log.log(
        logging.WARNING if recorded else logging.ERROR,
        "lineage_delivery_parked",
        extra={
            "event_id": event_id,
            "run_id": run_id,
            "reason": reason,
            # Which of the two this is, on the record rather than inferred from the level: an operator filtering a
            # dashboard needs the field, and the level alone cannot be queried.
            "already_recorded": recorded,
            # WHOSE provenance this was. `None` when the payload carries no verified sub: anonymous beats
            # misattributed (see `author_sub_from_payload`).
            "author": author,
        },
    )
    # A non-zero `DEAD_LETTERED` means the feed is missing that run, which is the claim an alert on it makes.
    record_outcome(Outcome.PARKED_ALREADY_RECORDED if recorded else Outcome.DEAD_LETTERED, door=Door.DEAD_LETTER)


async def _feed_holds(provenance: ProvenanceStore, envelope: Mapping[str, Any]) -> bool:
    """Whether the feed already holds the row this event writes. The event's own row, never its run's: a run's START
    reaching the feed says nothing of its COMPLETE, the event that names its outputs and so the one a missing grant
    refuses ([[LH-148]])."""
    key = delivery_feed_key(envelope)
    return key is not None and key in await provenance.events_recorded({key})


async def _keep(provenance: ProvenanceStore, envelope: Mapping[str, Any], *, event_id: str | None, run_id: str | None, author: str | None, reason: str) -> None:
    """Keep ``envelope`` in the ``refusals`` table under ``event_id``, its CloudEvent id, or, for one that carries none a
    key can hold, under the feed row it would write: the handling records an event whatever its envelope's id, so a
    replay must reach it too. An envelope that names neither holds no event the feed could ever record, and is only
    logged by its caller."""
    name = event_id or delivery_feed_key(envelope)
    if name is None:
        return
    await provenance.record_refusal(outbox_key=f"{PARKED_KEY_PREFIX}{name}", run_id=run_id, author=author, reason=reason, event_json=json.dumps(envelope))


__all__ = ["CONSUMER", "STREAM", "IngestLane", "lane", "park"]
