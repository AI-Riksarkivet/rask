"""#4 — durable object-store outbox for lineage events (the crash-window durability primitive)."""

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lakehouse.events import outbox
from lakehouse.events.cloud_events import TRACE_ATTRIBUTES, lineage_envelope
from lakehouse.process.correlation import continuing
from lakehouse.storage.options import StorageOptions
from tests.bus_double import BusDouble


def _uri(tmp_path: Any) -> str:
    return f"file://{tmp_path}/_lineage_outbox"


def test_drop_absent_is_idempotent(tmp_path: Any) -> None:
    outbox.drop_event(_uri(tmp_path), StorageOptions(), "ghost")  # no raise on a missing object


A_COMPLETE = {"eventType": "COMPLETE", "run": {"runId": "r1"}}


def _publish(bus: BusDouble, uri: str) -> None:
    """Publish :data:`A_COMPLETE` on the lineage lane through the outbox at ``uri``."""
    asyncio.run(
        outbox.publish_with_outbox(
            bus, lineage_envelope(A_COMPLETE), outbox_uri=uri, storage_options=StorageOptions(), topic="t", timeout_seconds=5, lane="lineage"
        )
    )


def test_publish_with_outbox_stages_then_drops_on_ack(tmp_path: Any) -> None:
    uri = _uri(tmp_path)
    bus = BusDouble()

    _publish(bus, uri)

    # The event rides its lane's envelope, under the id of this event and no other of its run; the trace attributes are
    # the publish's own (test_an_event_the_bus_did_not_take_stays_staged_as_its_envelope_in_the_trace_it_was_staged_under).
    assert [{key: value for key, value in sent.envelope.items() if key not in TRACE_ATTRIBUTES} for sent in bus.sent] == [
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


def test_an_event_the_bus_did_not_take_stays_staged_as_its_envelope_in_the_trace_it_was_staged_under(tmp_path: Any) -> None:
    """What the relay finds after a failed publish: the envelope, under its id, the event its producer made as ``data``,
    and the trace of the request that staged it (ADR 0209), which the relay's publish continues. The id carries the
    event's state, so a run's COMPLETE and its FAIL are two objects: staged under the run alone, the later would truncate
    the earlier, on a run whose Lance write had committed."""
    uri = _uri(tmp_path)
    request = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    with pytest.raises(outbox.UnpublishedEventError) as unpublished, continuing(request):
        _publish(BusDouble(failing=TimeoutError("the bus did not answer")), uri)

    staged = [(key, json.loads(body)) for key, body in outbox.list_events(uri, StorageOptions())]
    assert (unpublished.value.staged, [(key, envelope["data"], envelope["traceparent"]) for key, envelope in staged]) == (
        True,
        [("r1@COMPLETE", A_COMPLETE, request)],
    )


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
        outbox.stage_event(uri, StorageOptions(), f"run-{i}", event_json="{}")
    depth, age = outbox.backlog(uri, StorageOptions())
    assert depth == 3
    assert age >= 0.0  # a real, non-negative age for the OLDEST staged event


def test_list_events_is_bounded_and_oldest_first(tmp_path: Any) -> None:
    # The drain used to materialise the WHOLE prefix inside the single-flight lock, so a backlog — exactly
    # what the outbox exists to survive — could OOM/stall the tick. Cap it, oldest-first so nothing starves.
    uri = _uri(tmp_path)
    for i in range(5):
        outbox.stage_event(uri, StorageOptions(), f"run-{i}", event_json="{}")
        time.sleep(0.01)  # distinct mtimes so "oldest first" is actually assertable

    first_two = [rid for rid, _ in outbox.list_events(uri, StorageOptions(), limit=2)]
    assert first_two == ["run-0", "run-1"]  # bounded AND oldest-first (not arbitrary order)

    assert len({rid for rid, _ in outbox.list_events(uri, StorageOptions())}) == 5  # no limit => everything


#: One modification time, in nanoseconds, for objects a test stages as a store with whole-second times would hold them.
_ONE_TIME = 1_764_000_000 * 10**9


def _staged_at_one_time(root: Path, key: str) -> None:
    outbox.stage_event(_uri(root), StorageOptions(), key, event_json="{}")
    os.utime(root / "_lineage_outbox" / f"{key}.json", ns=(_ONE_TIME, _ONE_TIME))


def _drop_one_already_listed(root: Path, listed: list[str]) -> set[str]:
    outbox.drop_event(_uri(root), StorageOptions(), listed[0])
    return {listed[0]}


def _stage_one_more(root: Path, _listed: list[str]) -> set[str]:
    _staged_at_one_time(root, "0")
    return {"0"}


@pytest.mark.parametrize(
    "between_pages",
    [
        pytest.param(_drop_one_already_listed, id="an-object-the-first-page-listed-is-dropped"),
        pytest.param(_stage_one_more, id="an-object-is-staged-at-the-cursors-time"),
    ],
)
def test_a_paged_walk_lists_each_object_staged_throughout_it_once_though_all_share_one_modification_time(
    tmp_path: Path, between_pages: Callable[[Path, list[str]], set[str]]
) -> None:
    for key in ("a", "b", "c", "d"):
        _staged_at_one_time(tmp_path, key)

    page = outbox.page_events(_uri(tmp_path), StorageOptions(), limit=2)
    listed = [key for key, _ in page.events]
    came_or_went = between_pages(tmp_path, listed)
    for _ in range(4):  # more pages than the walk needs, so a cursor that never ends fails the count below
        if page.next is None:
            break
        page = outbox.page_events(_uri(tmp_path), StorageOptions(), limit=2, after=page.next)
        listed += [key for key, _ in page.events]

    assert sorted(key for key in listed if key not in came_or_went) == sorted({"a", "b", "c", "d"} - came_or_went)
