"""Unit tests for the control-plane change-event pipeline primitives (P1).

Infra-free: the shared event model and the
best-effort emitter (exercised on a double of the process's bus connection — it MUST swallow failures so a bus
outage never fails a catalog mutation). The emit sites, the broadcast subscription, and the poll endpoint are
covered by their own tests once wired; these pin the foundation.
"""

import asyncio
from typing import Any

from lakehouse.events.control_emit import (
    BusControlEmitter,
    NoopControlEmitter,
    emit_control,
    make_control_emitter,
)
from lakehouse.events.control_events import CONTROL_TOPIC, CatalogControlEvent, ControlAction
from tests.bus_double import BusDouble


def _evt(action: ControlAction = "grant_added", **over: Any) -> CatalogControlEvent:
    base: dict[str, Any] = {"object_type": "grant", "object_id": "table:db1$t", "actor": "user:alice"}
    base.update(over)
    return CatalogControlEvent(action=action, **base)


# ── emitter (best-effort publish on the bus) ────────────────────────────────────────────────────────────


def test_the_emitter_publishes_on_the_control_topic_under_the_events_own_id() -> None:
    bus = BusDouble()
    em = BusControlEmitter(bus, topic=CONTROL_TOPIC, timeout_seconds=5, service="catalog", sign=None)
    e = _evt()
    asyncio.run(em.emit(e))
    [sent] = bus.sent
    # The control lane's envelope, under the event's own id: a relay's copy of the event is stored once.
    assert (sent.topic, sent.message_id, sent.envelope["id"], sent.envelope["type"]) == (
        CONTROL_TOPIC,
        e.event_id,
        e.event_id,
        "rask.catalog.control.grant_added.v1",
    )
    assert CatalogControlEvent.model_validate(sent.event) == e  # pointer payload, round-trips


def test_make_control_emitter_selection() -> None:
    assert isinstance(make_control_emitter(enabled=True, bus=BusDouble(), timeout_seconds=5, service="catalog", sign=None), BusControlEmitter)
    # Off, or no connection to the bus → the no-op.
    assert isinstance(make_control_emitter(enabled=False, bus=BusDouble(), timeout_seconds=5, service="catalog", sign=None), NoopControlEmitter)
    assert isinstance(make_control_emitter(enabled=True, bus=None, timeout_seconds=5, service="catalog", sign=None), NoopControlEmitter)


# ── the emit helper the endpoints call ───────────────────────────────────────────────────────────────


def test_emit_control_builds_and_emits() -> None:
    bus = BusDouble()
    em = BusControlEmitter(bus, topic=CONTROL_TOPIC, timeout_seconds=5, service="catalog", sign=None)
    asyncio.run(
        emit_control(
            em,
            action="table_renamed",
            object_type="table",
            object_id="table:db1$t",
            actor="user:alice",
            extra={"from": "t", "to": "t2"},
        )
    )
    published = CatalogControlEvent.model_validate(bus.events[0])
    assert published.action == "table_renamed" and published.extra == {"from": "t", "to": "t2"}
