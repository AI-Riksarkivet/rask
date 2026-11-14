"""The catalog's own control-plane change events, followed by each replica into its ring buffer.

The catalog publishes ``CatalogControlEvent``s on ``catalog.control.v1`` over its own connection to the bus
(``lakehouse.control_emit``), and each replica **follows** that topic here on the same connection, by reading its
stream by sequence (`lakehouse.bus.Bus.follow`). So **every** catalog replica receives
**every** event, in the stream's order, and none takes one from another; each appends it to its own per-replica ring
buffer (``core/control_buffer.py``). The poll endpoint (``GET /management/v1/events``) then serves each replica's
buffer to the console. The lane runs **only** when control eventing is enabled (``control_emit_enabled``, matching the
emitter gate in ``main.py``), so a control-off deployment follows nothing.

**It acts only on what a control signer signed** ([[CTL-32]], ADR 0171). Every user the bus lets publish on the topic
reaches this lane, so the actor an event names is a claim until its ``rask_signature`` proves it. The event is
verified with the control doors' own rule, against the identities of the role ``control_signer_role`` names for its
action (``RASK_CONTROL_SIGNER_ROLES``), under ``RASK_SIGNATURE_DOORS``: ``enforce`` buffers nothing for a refusal and
evicts nothing for it, ``observe`` acts as ``off`` and counts what it would refuse. A refusal is counted in
``catalog.signature.refused``.

**A followed event is handed on once**, so an event this replica cannot judge is not delivered again: while the secret
store does not serve the keys a signature is checked against, the event is judged again here a few times, and then
given up as not handled (`KeysUnavailableError`), which the lane counts (``bus.deliveries`` at ``failed``). The buffer
is a hint (drop-oldest, and ``reset`` on a cursor gap), and the audit trail is the durable record: what a lost hint
costs a client is a read it did not know to repeat.

**The lane waits for keys once, not once per event.** The follow hands on one event at a time, so a wait holds back
every event behind it, on every replica, and any publisher on the topic can send an event whose keys cannot be read at
once (`ControlLane._judged_once_keys_answer`). After a wait, an event whose keys cannot be read is given up on its one
judgement until `_WAIT_AGAIN_SECONDS` have passed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final, get_args

from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import ValidationError

from catalog.api.control_relay import bounded, published_keys
from lakehouse.bus_delivery import Delivery
from lakehouse.control_events import CONTROL_TOPIC, CatalogControlEvent, control_signer_role
from lakehouse.lineage.door import DoorMode, DoorVerdict, judge
from lakehouse.lineage.signing import RefusalReason, control_signature_of, verify_control_signature


if TYPE_CHECKING:
    from fastapi import FastAPI

    from catalog.core.config import Settings


log = logging.getLogger(__name__)

#: The stream the control topic is kept on, as the chart's stream Job declares it.
STREAM: Final = "CATALOG_CONTROL"
#: The topic each replica follows.
TOPIC: Final = CONTROL_TOPIC
#: How many times an event is judged while the keys its signature is checked against cannot be read, and how far apart.
_KEY_READS: Final = 5
_KEY_READ_SECONDS: Final = 15.0
#: How long after the lane has waited for keys it does not wait again. Its waits then hold the follow at most one part
#: in six (`_KEY_READS` judgements `_KEY_READ_SECONDS` apart, a minute, of every six).
_WAIT_AGAIN_SECONDS: Final = 300.0

_meter = metrics.get_meter("lance.catalog")
_signature_refused = _meter.create_counter(
    "catalog.signature.refused",
    unit="{event}",
    description="Control events the catalog's enforcing lane refused, by reason: buffered nowhere and evicting nothing.",
)
_signature_would_refuse = _meter.create_counter(
    "catalog.signature.would_refuse",
    unit="{event}",
    description="Control events the catalog's observing lane acted on that enforce would refuse, by reason (keys_unavailable: the keys could not be read).",
)


class KeysUnavailableError(RuntimeError):
    """The keys a control event's signature is checked against could not be read, so the event was not judged."""


def create_signature_refused_series() -> None:
    """Create every reason series of `catalog.signature.refused` at 0.

    The alert over this counter is `rate(...) > 0`, and a cumulative counter exports nothing for a series before its first
    add: a series born at 1 has no 0 before it, so `rate()` sees no increase and the first refusal never pages. The catalog
    runs under `opentelemetry-instrument`, whose MeterProvider is installed before this module is imported.
    """
    for reason in get_args(RefusalReason.__value__):
        _signature_refused.add(0, {"lance.catalog.reason": reason})


class ControlLane:
    """What a catalog replica does with each control event it follows: buffers the ones a control signer signed."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        #: When the lane last ended a wait for keys, on its loop's clock; None while it never has.
        self._waited_at: float | None = None

    async def handle(self, delivery: Delivery) -> None:
        """Buffer the control event ``delivery`` carries into this replica's ring buffer.

        The envelope's ``data`` is the ``CatalogControlEvent``, as its publisher built it (`lakehouse.cloud_events`). A
        malformed event is **dropped**: events are hints and the audit trail is the durable record. A valid one is
        appended (``event_id`` dedupes a repeat), so a drop only costs the client a read it did not know to repeat.

        Raises:
            KeysUnavailableError: The keys the event's signature is checked against could not be read; it is buffered
                nowhere.
        """
        data = (delivery.envelope() or {}).get("data")
        try:
            event = CatalogControlEvent.model_validate(data)
        except (ValidationError, TypeError, ValueError) as exc:
            log.warning("control_event_invalid", extra={"error": str(exc)})
            return
        # Screened whatever its shape: a payload that is not a mapping carries no signature, so it is judged unsigned.
        if await self._admitted(data if isinstance(data, dict) else {}, event):
            self._app.state.control_buffer.append(event)

    async def _admitted(self, arrived: dict[str, Any], event: CatalogControlEvent) -> bool:
        """Whether the lane may act on ``event``, judged over ``arrived``: the event's data exactly as delivered, since a
        model dump grows the fields a schema defaults, and the signer signed what it wrote.

        Raises:
            KeysUnavailableError: The keys could not be read: on any of ``_KEY_READS`` tries, or on the one judgement an
                event gets while the lane waited for keys moments ago.
        """
        logged = {"event_id": bounded(event.event_id), "action": event.action}
        verdict = await self._judged(arrived, event)
        if verdict.retry:
            verdict = await self._judged_once_keys_answer(arrived, event, logged)
        return self._counted(verdict, arrived, logged)

    async def _judged_once_keys_answer(self, arrived: dict[str, Any], event: CatalogControlEvent, logged: dict[str, Any]) -> DoorVerdict:
        """The verdict on an event whose keys could not be read, judged again every ``_KEY_READ_SECONDS`` until they are.

        A WAIT HERE HOLDS BACK EVERY EVENT BEHIND THIS ONE. Keys cannot be read while the store does not answer, and
        also when an event names a key id the list read moments ago does not hold: the reader reads again for such an id
        once per interval (`lakehouse.lineage.keys.KEY_REFRESH_INTERVAL_SECONDS`), and a publisher can name an id it
        made up in every event it sends. One wait rides out a store away for a moment, and a key its signer began to use
        moments ago, after which the list read holds it for every event behind. A wait for each such event would hold
        the lane for as long as a run of them lasts, so once the lane has waited, an event whose keys cannot be read is
        given up at once until ``_WAIT_AGAIN_SECONDS`` have passed.

        Raises:
            KeysUnavailableError: The keys could not be read on any try, or the lane waited for keys moments ago.
        """
        clock = asyncio.get_running_loop().time
        named = bounded(event.event_id)
        if self._waited_at is not None and clock() - self._waited_at < _WAIT_AGAIN_SECONDS:
            raise KeysUnavailableError(f"the keys control event {named} is checked against could not be read, and the lane waited for keys moments ago")
        log.warning("catalog_signature_keys_unavailable", extra=logged)
        try:
            for _ in range(_KEY_READS - 1):
                await asyncio.sleep(_KEY_READ_SECONDS)
                verdict = await self._judged(arrived, event)
                if not verdict.retry:
                    return verdict
        finally:
            self._waited_at = clock()
        raise KeysUnavailableError(f"the keys control event {named} is checked against could not be read on {_KEY_READS} tries")

    async def _judged(self, arrived: dict[str, Any], event: CatalogControlEvent) -> DoorVerdict:
        mode = self._settings.signature_doors
        if mode is DoorMode.OFF:
            return DoorVerdict(act=True)
        signers = self._settings.control_signer_roles.get(control_signer_role(event.action), frozenset())
        claim = control_signature_of(arrived)
        source = published_keys(self._app).for_event(claim.kid if claim else None)
        return await run_in_threadpool(
            judge, lambda: verify_control_signature(arrived, source=source, signers=signers, delegators=self._settings.event_delegators), mode
        )

    @staticmethod
    def _counted(verdict: DoorVerdict, arrived: dict[str, Any], logged: dict[str, Any]) -> bool:
        """Count and say what ``verdict`` refuses or would refuse; whether the lane acts on the event."""
        claim = control_signature_of(arrived)
        named = {**logged, "identity": bounded(claim.identity) if claim else None}
        if verdict.observed is not None:
            _signature_would_refuse.add(1, {"lance.catalog.reason": verdict.observed})
            log.warning("catalog_signature_would_refuse", extra={**named, "reason": verdict.observed})
        if verdict.refused is not None:
            _signature_refused.add(1, {"lance.catalog.reason": verdict.refused})
            log.warning("catalog_signature_refused", extra={**named, "reason": verdict.refused})
        return verdict.act


__all__ = ["STREAM", "TOPIC", "ControlLane", "KeysUnavailableError", "create_signature_refused_series"]
