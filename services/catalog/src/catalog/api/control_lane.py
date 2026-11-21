"""The catalog's own control-plane change events: each replica judges every one it follows, and counts what it refuses.

The catalog publishes ``CatalogControlEvent``s on ``catalog.control.v1`` over its own connection to the bus
(``lakehouse.events.control_emit``), and each replica **follows** that topic here on the same connection, by reading its
stream by sequence (`lakehouse.events.bus.Bus.follow`). The lane runs **only** when control eventing is enabled
(``control_emit_enabled``, matching the emitter gate in ``main.py``), so a control-off deployment follows nothing. What
the console reads is served from the same stream at its poll (``GET /management/v1/events``,
``api/v1/endpoints/events.py``), judged by the same rule (:func:`judge`), so no replica holds what another must serve.

**The lane is the refusals' alarm** ([[CTL-32]], ADR 0171). Every user the bus lets publish on the topic reaches the
stream, so the actor an event names is a claim until its ``rask_signature`` proves it. The lane judges each event with
the control doors' own rule, against the identities of the role ``control_signer_role`` names for its action
(``RASK_CONTROL_SIGNER_ROLES``), under ``RASK_SIGNATURE_DOORS``, and counts a refusal in ``catalog.signature.refused``
(``observe`` counts what it would refuse in ``catalog.signature.would_refuse``), whether or not a console polls.

**The lane waits for keys once, not once per event.** Keys cannot be read while the store does not answer, and also
when an event names a key id the list read moments ago does not hold: the reader reads again for such an id once per
interval (`lakehouse.events.published_keys.KEY_REFRESH_INTERVAL_SECONDS`), so a made-up key id is refused, and counted, only once
the lane has waited for that read. The follow hands on one event at a time, so a wait holds back every event behind it:
an event whose keys still cannot be read after ``_KEY_READS`` judgements is given up as not handled
(`KeysUnavailableError`, counted by the follow as ``bus.deliveries`` at ``failed``), and after a wait, one is given up on
its one judgement until ``_WAIT_AGAIN_SECONDS`` have passed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Final, get_args

from fastapi.concurrency import run_in_threadpool
from opentelemetry import metrics
from pydantic import ValidationError

from catalog.api.control_relay import bounded, published_keys
from lakehouse.events.bus_delivery import Delivery
from lakehouse.events.control_events import CONTROL_TOPIC, CatalogControlEvent, control_signer_role
from lakehouse.events.door_verdict import DoorMode, DoorVerdict
from lakehouse.events.door_verdict import judge as judge_door
from lakehouse.events.signing import RefusalReason, control_signature_of, verify_control_signature


if TYPE_CHECKING:
    from fastapi import FastAPI

    from catalog.core.config import Settings, SignatureDoorSettings


log = logging.getLogger(__name__)

#: The stream the control topic is kept on, as the chart's stream Job declares it.
STREAM: Final = "CATALOG_CONTROL"
#: The topic each replica follows.
TOPIC: Final = CONTROL_TOPIC
#: How many times an event is judged while the keys its signature is checked against cannot be read, and how far apart:
#: past the key reader's own interval, so the last judgement reads the list again.
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


def control_event_in(delivery: Delivery) -> tuple[dict[str, Any], CatalogControlEvent] | None:
    """The control event ``delivery`` carries, with its data exactly as delivered (what a signature is checked over), or
    None for a message that carries none (logged): a malformed event is passed over. Data that is not a mapping carries
    no signature, so it is judged unsigned."""
    data = (delivery.envelope() or {}).get("data")
    try:
        event = CatalogControlEvent.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        log.warning("control_event_invalid", extra={"error": str(exc)})
        return None
    return (data if isinstance(data, dict) else {}), event


async def judge(app: FastAPI, settings: SignatureDoorSettings, arrived: dict[str, Any], *, event: CatalogControlEvent) -> DoorVerdict:
    """The control doors' verdict on ``event``, judged over ``arrived``: the event's data exactly as delivered, since a
    model dump grows the fields a schema defaults, and the signer signed what it wrote. ``retry`` when the keys the
    signature is checked against could not be read."""
    mode = settings.signature_doors
    if mode is DoorMode.OFF:
        return DoorVerdict(act=True)
    signers = settings.control_signer_roles.get(control_signer_role(event.action), frozenset())
    claim = control_signature_of(arrived)
    source = published_keys(app).for_event(claim.kid if claim else None)
    return await run_in_threadpool(
        judge_door, lambda: verify_control_signature(arrived, source=source, signers=signers, delegators=settings.event_delegators), mode
    )


class ControlLane:
    """What a catalog replica does with each control event it follows: judges it, and counts what it refuses."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self._app = app
        self._settings = settings
        #: When the lane last ended a wait for keys, on its loop's clock; None while it never has.
        self._waited_at: float | None = None

    async def handle(self, delivery: Delivery) -> None:
        """Judge the control event ``delivery`` carries and count what the door refuses or would refuse.

        Raises:
            KeysUnavailableError: The keys could not be read: on any of ``_KEY_READS`` tries, or on the one judgement an
                event gets while the lane waited for keys moments ago.
        """
        carried = control_event_in(delivery)
        if carried is None:
            return
        arrived, event = carried
        logged = {"event_id": bounded(event.event_id), "action": event.action}
        verdict = await judge(self._app, self._settings, arrived, event=event)
        if verdict.retry:
            verdict = await self._judged_once_keys_answer(arrived, event, logged)
        self._counted(verdict, arrived, logged)

    async def _judged_once_keys_answer(self, arrived: dict[str, Any], event: CatalogControlEvent, logged: dict[str, Any]) -> DoorVerdict:
        """The verdict on an event whose keys could not be read, judged again every ``_KEY_READ_SECONDS`` until they are.

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
                verdict = await judge(self._app, self._settings, arrived, event=event)
                if not verdict.retry:
                    return verdict
        finally:
            self._waited_at = clock()
        raise KeysUnavailableError(f"the keys control event {named} is checked against could not be read on {_KEY_READS} tries")

    @staticmethod
    def _counted(verdict: DoorVerdict, arrived: dict[str, Any], logged: dict[str, Any]) -> None:
        """Count and say what ``verdict`` refuses or would refuse."""
        claim = control_signature_of(arrived)
        named = {**logged, "identity": bounded(claim.identity) if claim else None}
        if verdict.observed is not None:
            _signature_would_refuse.add(1, {"lance.catalog.reason": verdict.observed})
            log.warning("catalog_signature_would_refuse", extra={**named, "reason": verdict.observed})
        if verdict.refused is not None:
            _signature_refused.add(1, {"lance.catalog.reason": verdict.refused})
            log.warning("catalog_signature_refused", extra={**named, "reason": verdict.refused})


__all__ = ["STREAM", "TOPIC", "ControlLane", "KeysUnavailableError", "control_event_in", "create_signature_refused_series", "judge"]
