"""Unit tests for the control-plane change-event pipeline primitives (P1).

Infra-free: the shared event model, the per-replica ring buffer's cursor/reset/dedupe semantics, and the
best-effort Dapr emitter (exercised with a fake sidecar client — it MUST swallow failures so a bus outage
never fails a catalog mutation). The emit sites, the broadcast subscription, and the poll endpoint are
covered by their own tests once wired; these pin the foundation.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from catalog.core.control_buffer import ControlEventBuffer
from catalog.core.control_emit import (
    DaprControlEmitter,
    NoopControlEmitter,
    emit_control,
    make_control_emitter,
)

from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent, ControlAction


class _FakeDapr:
    """A stand-in Dapr client: records publishes, or raises to prove the emitter is best-effort."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def publish_event(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("sidecar down")
        self.calls.append(kwargs)


def _evt(action: ControlAction = "grant_added", **over: Any) -> CatalogControlEvent:
    base: dict[str, Any] = {"object_type": "grant", "object_id": "table:db1$t", "actor": "user:alice"}
    base.update(over)
    return CatalogControlEvent(action=action, **base)


# ── model ──────────────────────────────────────────────────────────────────────────────────────────


def test_event_defaults_and_roundtrip() -> None:
    e = _evt(extra={"relation": "can_read_data", "subject": "user:bob"})
    assert e.event_id and len(e.event_id) == 32  # a uuid4 hex, auto-assigned
    assert e.occurred_at.tzinfo is not None  # UTC-aware
    # Wire round-trip: the JSON the emitter publishes re-parses to an equal event (the consumer contract).
    assert CatalogControlEvent.model_validate_json(e.model_dump_json()) == e


# ── ring buffer ────────────────────────────────────────────────────────────────────────────────────


def test_buffer_zero_cursor_returns_window() -> None:
    buf = ControlEventBuffer(8)
    buf.append(_evt(object_id="a"))
    buf.append(_evt(object_id="b"))
    # since=0 returns the whole retained window (the recent activity a fresh client renders on connect), NOT
    # an empty baseline — a baseline would advance the cursor past these first events and silently skip them.
    events, head, reset = buf.since(0)
    assert [e.object_id for e in events] == ["a", "b"] and head == 2 and reset is False


def test_buffer_cursor_delivers_only_new() -> None:
    buf = ControlEventBuffer(8)
    buf.append(_evt(object_id="a"))
    buf.append(_evt(object_id="b"))
    _, head_after_two, _ = buf.since(0)
    buf.append(_evt(object_id="c"))
    events, head, reset = buf.since(head_after_two)
    assert [e.object_id for e in events] == ["c"] and head == 3 and reset is False


def test_buffer_dedupe_redelivery() -> None:
    buf = ControlEventBuffer(8)
    e = _evt()
    buf.append(e)
    buf.append(e)  # same event_id (a Dapr redelivery) → ignored, no new cursor
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


def test_buffer_drops_oldest_when_full() -> None:
    buf = ControlEventBuffer(2)
    for i in range(5):  # cursors 1..5; only the last two (4,5) survive the maxlen-2 ring
        buf.append(_evt(object_id=str(i)))
    events, head, reset = buf.since(3)  # only cursors 4,5 retained; wanted 4.. → gets cursor 4 (object "3")
    assert [e.object_id for e in events] == ["3", "4"] and head == 5 and reset is False


# ── emitter (best-effort Dapr publish) ───────────────────────────────────────────────────────────────


def test_noop_emitter_is_total() -> None:
    asyncio.run(NoopControlEmitter().emit(_evt()))  # no raise, no side effect


def test_dapr_emitter_publishes_control_topic() -> None:
    fake = _FakeDapr()
    em = DaprControlEmitter(cast(Any, fake), pubsub="catalog-control-pubsub", topic=CONTROL_TOPIC, timeout_seconds=5)
    e = _evt()
    asyncio.run(em.emit(e))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["pubsub_name"] == "catalog-control-pubsub"
    assert call["topic_name"] == CONTROL_TOPIC
    assert call["data_content_type"] == "application/json"
    assert CatalogControlEvent.model_validate_json(call["data"]) == e  # pointer payload, round-trips


def test_dapr_emitter_best_effort_swallows() -> None:
    em = DaprControlEmitter(cast(Any, _FakeDapr(fail=True)), pubsub="p", topic=CONTROL_TOPIC, timeout_seconds=5)
    asyncio.run(em.emit(_evt()))  # a wedged/failing sidecar must NOT raise into the mutation path


def test_make_control_emitter_selection() -> None:
    assert isinstance(
        make_control_emitter(enabled=True, dapr=cast(Any, _FakeDapr()), pubsub="p", timeout_seconds=5),
        DaprControlEmitter,
    )
    # Off, or no sidecar client → the no-op.
    assert isinstance(
        make_control_emitter(enabled=False, dapr=cast(Any, _FakeDapr()), pubsub="p", timeout_seconds=5),
        NoopControlEmitter,
    )
    assert isinstance(
        make_control_emitter(enabled=True, dapr=None, pubsub="p", timeout_seconds=5),
        NoopControlEmitter,
    )


# ── the emit helper the endpoints call ───────────────────────────────────────────────────────────────


def test_emit_control_builds_and_emits() -> None:
    fake = _FakeDapr()
    em = DaprControlEmitter(cast(Any, fake), pubsub="p", topic=CONTROL_TOPIC, timeout_seconds=5)
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
    published = CatalogControlEvent.model_validate_json(fake.calls[0]["data"])
    assert published.action == "table_renamed" and published.extra == {"from": "t", "to": "t2"}


def test_emit_control_noop_emitter_is_safe() -> None:
    # The off state (the ControlEmitterDep fallback) is a NoopControlEmitter — emit_control on it never raises
    # and never publishes, so a control-off / unwired deployment can call it freely at every mutation site.
    asyncio.run(emit_control(NoopControlEmitter(), action="grant_added", object_type="grant", object_id="x", actor=None))  # no raise, no publish
