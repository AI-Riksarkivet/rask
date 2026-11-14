"""Unit tests for the control-plane change-event pipeline primitives (P1).

Infra-free: the shared event model, the per-replica ring buffer's cursor/reset/dedupe semantics, and the
best-effort emitter (exercised on a double of the process's bus connection — it MUST swallow failures so a bus
outage never fails a catalog mutation). The emit sites, the broadcast subscription, and the poll endpoint are
covered by their own tests once wired; these pin the foundation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from catalog.core.control_buffer import ControlEventBuffer
from lakehouse.control_emit import (
    BusControlEmitter,
    NoopControlEmitter,
    emit_control,
    make_control_emitter,
)
from lakehouse.control_events import CONTROL_TOPIC, CatalogControlEvent, ControlAction
from tests.bus_double import BusDouble


def _evt(action: ControlAction = "grant_added", **over: Any) -> CatalogControlEvent:
    base: dict[str, Any] = {"object_type": "grant", "object_id": "table:db1$t", "actor": "user:alice"}
    base.update(over)
    return CatalogControlEvent(action=action, **base)


# ── ring buffer ────────────────────────────────────────────────────────────────────────────────────


def test_buffer_zero_cursor_returns_window() -> None:
    buf = ControlEventBuffer(8)
    buf.append(_evt(object_id="a"))
    buf.append(_evt(object_id="b"))
    # since=0 returns the whole retained window (the recent activity a fresh client renders on connect), NOT
    # an empty baseline — a baseline would advance the cursor past these first events and silently skip them.
    events, head, reset = buf.since(0)
    assert [e.object_id for e in events] == ["a", "b"] and head == 2 and reset is False


def test_buffer_dedupe_redelivery() -> None:
    buf = ControlEventBuffer(8)
    e = _evt()
    buf.append(e)
    buf.append(e)  # same event_id (a redelivery) → ignored, no new cursor
    assert buf.head == 1
    buf.append(_evt(object_id="other"))  # a distinct event advances
    assert buf.head == 2


def test_buffer_overflow_signals_reset() -> None:
    buf = ControlEventBuffer(2)  # tiny ring
    buf.append(_evt(object_id="1"))  # cursor 1
    buf.append(_evt(object_id="2"))  # cursor 2
    buf.append(_evt(object_id="3"))  # cursor 3 → evicts cursor 1 (oldest retained is now 2)
    # A client last at cursor 1 wants cursor 2..; oldest retained IS 2, so no gap → they get 2,3 (no reset).
    events, head, reset = buf.since(1)
    assert [e.object_id for e in events] == ["2", "3"] and head == 3 and reset is False
    buf.append(_evt(object_id="4"))  # cursor 4 → evicts 2 (oldest retained is now 3)
    # The same client (last cursor 1) now wants 2..; oldest is 3 → (1+1)=2 < 3 → it missed the gap → reset.
    _, head2, reset2 = buf.since(1)
    assert reset2 is True and head2 == 4


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
