"""#4 — durable object-store outbox for lineage events (the crash-window durability primitive)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from lakehouse import outbox
from lakehouse.cloud_events import lineage_envelope
from lakehouse.storage_options import StorageOptions
from tests.bus_double import BusDouble


def _uri(tmp_path: Any) -> str:
    return f"file://{tmp_path}/_lineage_outbox"


def test_drop_absent_is_idempotent(tmp_path: Any) -> None:
    outbox.drop_event(_uri(tmp_path), StorageOptions(), "ghost")  # no raise on a missing object


A_COMPLETE = {"eventType": "COMPLETE", "run": {"runId": "r1"}}


def _publish(bus: BusDouble, uri: str) -> None:
    """Publish :data:`A_COMPLETE` on the lineage lane through the outbox at ``uri``."""
    asyncio.run(outbox.publish_with_outbox(bus, lineage_envelope(A_COMPLETE), outbox_uri=uri, storage_options=StorageOptions(), topic="t", timeout_seconds=5))


def test_publish_with_outbox_stages_then_drops_on_ack(tmp_path: Any) -> None:
    uri = _uri(tmp_path)
    bus = BusDouble()

    _publish(bus, uri)

    # The event rides its lane's envelope, under the id of this event and no other of its run.
    assert [sent.envelope for sent in bus.sent] == [
        {
            "specversion": "1.0",
            "id": "r1@COMPLETE",
            "source": "rask/lineage",
            "type": "rask.lineage.run.complete.v1",
            "datacontenttype": "application/json",
            "data": A_COMPLETE,
        }
    ]
    assert list(outbox.list_events(uri, StorageOptions())) == []  # dropped after the publish acked


def test_a_failed_stage_still_attempts_the_publish(tmp_path: Any, monkeypatch: Any) -> None:
    # Staging is a DURABILITY aid, not a precondition for delivery. Raising past the publish turns a
    # transient object-store blip into the one outcome the outbox exists to prevent: an event that
    # reaches nobody. An unstaged event that reaches the bus is delivered; an unstaged event that is
    # never published is gone, so publishing anyway strictly dominates.
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("outbox bucket unreachable")

    monkeypatch.setattr(outbox, "stage_event", _boom)
    bus = BusDouble()

    _publish(bus, _uri(tmp_path))

    assert bus.events == [A_COMPLETE]


def test_an_event_the_bus_did_not_take_stays_staged_under_its_envelopes_id_as_the_event_alone(tmp_path: Any) -> None:
    """What the relay finds after a failed publish: the event its producer made, never its envelope, under its
    envelope's id. That id carries the event's state, so a run's COMPLETE and its FAIL are two objects: staged under
    the run alone, the later would truncate the earlier, on a run whose Lance write had committed."""
    uri = _uri(tmp_path)

    with pytest.raises(outbox.UnpublishedEventError) as unpublished:
        _publish(BusDouble(failing=TimeoutError("the bus did not answer")), uri)

    staged = [(key, json.loads(body)) for key, body in outbox.list_events(uri, StorageOptions())]
    assert (unpublished.value.staged, staged) == (True, [("r1@COMPLETE", A_COMPLETE)])


def test_a_delivered_event_whose_drop_fails_stays_staged_and_says_why(tmp_path: Any, monkeypatch: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The event reached the bus, so nothing is lost: its staged copy waits for the relay, which re-ingests and drops it.
    The failure is reported, as the publish's own is, since a store refusing every drop has each event delivered twice
    ([[LH-572]])."""

    def _refused(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the store refused the delete")

    monkeypatch.setattr(outbox, "drop_event", _refused)
    uri = _uri(tmp_path)
    bus = BusDouble()

    with caplog.at_level("WARNING", logger=outbox.__name__):
        _publish(bus, uri)

    assert (bus.events, len(list(outbox.list_events(uri, StorageOptions())))) == ([A_COMPLETE], 1)
    assert [(record.getMessage(), getattr(record, "error", None)) for record in caplog.records] == [("outbox_drop_failed", "the store refused the delete")]


# --------------------------------------------------------------------------- #
# BOUNDED DRAIN + SATURATION SNAPSHOT (GOAL-prove-it P1.1/P1.2)
# --------------------------------------------------------------------------- #


def test_backlog_reports_depth_and_oldest_age(tmp_path: Any) -> None:
    # The alertable pair. An outbox that silently stops draining — the one failure that loses lineage
    # forever — used to look exactly like a healthy one, because NOTHING was measured.
    uri = _uri(tmp_path)
    assert outbox.backlog(uri, StorageOptions()) == (0, 0.0)  # empty must report 0, not go stale
    for i in range(3):
        outbox.stage_event(uri, StorageOptions(), f"run-{i}", "{}")
    depth, age = outbox.backlog(uri, StorageOptions())
    assert depth == 3
    assert age >= 0.0  # a real, non-negative age for the OLDEST staged event


def test_list_events_is_bounded_and_oldest_first(tmp_path: Any) -> None:
    # The drain used to materialise the WHOLE prefix inside the single-flight lock, so a backlog — exactly
    # what the outbox exists to survive — could OOM/stall the tick. Cap it, oldest-first so nothing starves.
    uri = _uri(tmp_path)
    for i in range(5):
        outbox.stage_event(uri, StorageOptions(), f"run-{i}", "{}")
        time.sleep(0.01)  # distinct mtimes so "oldest first" is actually assertable

    first_two = [rid for rid, _ in outbox.list_events(uri, StorageOptions(), limit=2)]
    assert first_two == ["run-0", "run-1"]  # bounded AND oldest-first (not arbitrary order)

    assert len({rid for rid, _ in outbox.list_events(uri, StorageOptions())}) == 5  # no limit => everything
