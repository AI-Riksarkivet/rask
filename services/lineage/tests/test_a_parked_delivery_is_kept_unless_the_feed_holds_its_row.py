"""A delivery lineage's lane parks is kept for the estate admin's replay unless the feed already holds its row ([[LH-148]]).

The stream drops a message past its retention and nothing reads an ended one back, so the lane keeps a park in the
provenance namespace's ``refusals`` table, under its CloudEvent id, where `POST /admin/dlq/parked/{run_id}/replay`
re-presents it once its cause is fixed (`tests/integration/test_a_repaired_park_is_replayed_into_the_feed.py`).

`DEAD_LETTERED` MEANS THE FEED IS MISSING THE ROW, which is the claim an alert on it makes. Measured against the live
estate 2026-09-13: 8,515 parked deliveries of `lineage.events.v1` while the stream's last sequence was 5,860, and 17 of
21 distinct parked run ids sampled were already recorded. A feed row is written under its key, so a park of a row the
feed holds is visibility, not loss: it keeps nothing, logs at WARNING and is counted apart.

A PARK IS THE LAST WORD ON A DELIVERY, so one the store does not take is not taken: the delivery is asked for again and
parked then. A feed that cannot say whether it holds the row is such a park, since answering "nothing was lost" there
would silence the counter exactly when it matters.

A PARK DOES NOT FAIL FOR WHAT ITS DELIVERY CARRIES, since a park that fails is asked for again on every delivery after:
an id, a run or an author that is no plain text is left out of the row's key and the log line, and the delivery is kept
all the same.

Driven through lineage's own app and lifespan, each delivery handed to its ingest lane as the bus would hand it, its
counter read through a real in-memory reader.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from lineage_lane import Feed, Kept, measured, outcome, started

from lakehouse.bus_delivery import Outcome, Settlement
from lineage.core.config import get_settings
from lineage.services.provenance import delivery_feed_key


#: A delivery whose event the feed's write has not taken: the envelope the bus carries, its OpenLineage event in `data`.
_ENVELOPE: dict[str, Any] = {
    "id": "evt-1",
    "data": {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-22T12:00:00Z",
        "producer": "https://rask/test",
        "job": {"namespace": "rask", "name": "probe"},
        "run": {"runId": "run-lost", "facets": {"rask_author": {"sub": "alice"}}},
        "inputs": [],
        "outputs": [],
    },
}

#: The same event from a publisher whose envelope carries no CloudEvent id: the handling records it all the same.
_WITHOUT_AN_ID: dict[str, Any] = {"data": _ENVELOPE["data"]}


type Counts = Callable[[str], dict[frozenset[tuple[str, str]], int]]


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> Counts:
    """`lineage.events.processed`, read through a real in-memory reader."""
    return measured(monkeypatch, "_events_processed")


def _parked_on_its_last_delivery(monkeypatch: pytest.MonkeyPatch, feed: Feed, envelope: dict[str, Any] = _ENVELOPE) -> tuple[Settlement, int]:
    """Deliver ``envelope`` as the lane's last delivery of it, into a feed whose write fails, so its handling asks for it
    again and the lane parks it; answers the settlement and the lane's attempts."""
    with started(monkeypatch, feed) as lineage:
        attempts = get_settings().delivery_attempts
        return lineage.deliver(envelope, attempt=attempts), attempts


_UNDER_ITS_FEED_ROW = "dead_letter:run-lost@COMPLETE"


@pytest.mark.parametrize(
    ("envelope", "kept_under", "named"),
    [
        pytest.param(_ENVELOPE, "dead_letter:evt-1", "evt-1", id="under-its-cloudevent-id"),
        pytest.param(_WITHOUT_AN_ID, _UNDER_ITS_FEED_ROW, None, id="under-its-feed-row-when-its-envelope-carries-no-id"),
        pytest.param({**_ENVELOPE, "id": ["evt", 1]}, _UNDER_ITS_FEED_ROW, None, id="under-its-feed-row-when-its-id-is-no-text"),
        pytest.param({**_ENVELOPE, "id": "evt-\ud800"}, _UNDER_ITS_FEED_ROW, None, id="under-its-feed-row-when-its-id-has-no-utf-8-form"),
        pytest.param({**_ENVELOPE, "id": "e" * 513}, _UNDER_ITS_FEED_ROW, None, id="under-its-feed-row-when-its-id-is-longer-than-a-key-carries"),
    ],
)
def test_a_park_the_feed_does_not_hold_is_kept_logged_as_an_error_and_counted_as_loss(  # noqa: PLR0913 — the case, and what reads its row, log line and count
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, counts: Counts, envelope: dict[str, Any], kept_under: str, named: str | None
) -> None:
    feed = Feed(unrecordable=True)

    with caplog.at_level(logging.WARNING, logger="lineage.api.ingest_lane"):
        settled, attempts = _parked_on_its_last_delivery(monkeypatch, feed, envelope)

    assert settled == Settlement(outcome=Outcome.PARKED)
    assert feed.kept == {
        kept_under: Kept(
            run_id="run-lost", author="alice", reason=f"asked for again on its last delivery ({attempts} of {attempts})", event_json=json.dumps(envelope)
        )
    }
    [parked] = [record for record in caplog.records if record.message == "lineage_delivery_parked"]
    assert (parked.levelno, getattr(parked, "event_id", None), getattr(parked, "already_recorded", None), getattr(parked, "author", None)) == (
        logging.ERROR,
        named,
        False,
        "alice",
    )
    assert counts("lineage.events.processed") == {outcome("retried", "subscriber"): 1, outcome("dead_lettered", "dead_letter"): 1}


def test_a_park_whose_row_the_feed_holds_keeps_nothing_and_is_counted_apart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, counts: Counts
) -> None:
    """The two answers send an operator to different places, so they share no severity: an ERROR per re-park is what let
    the real losses scroll away among them."""
    key = delivery_feed_key(_ENVELOPE)
    assert key is not None, "the delivery names no feed row, so this tests nothing"
    feed = Feed(held={key: _ENVELOPE["data"]}, unrecordable=True)

    with caplog.at_level(logging.WARNING, logger="lineage.api.ingest_lane"):
        settled, _ = _parked_on_its_last_delivery(monkeypatch, feed)

    assert settled == Settlement(outcome=Outcome.PARKED)
    assert feed.kept == {}
    [parked] = [record for record in caplog.records if record.message == "lineage_delivery_parked"]
    assert (parked.levelno, getattr(parked, "already_recorded", None), getattr(parked, "author", None)) == (logging.WARNING, True, "alice")
    assert counts("lineage.events.processed") == {outcome("retried", "subscriber"): 1, outcome("parked_already_recorded", "dead_letter"): 1}


def test_a_delivery_parked_unhandled_is_kept_whatever_its_payload_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivery past the lane's last is parked without being handled, so nothing has read its payload as an event:
    a run id no table's writer takes is left out of its row, and the delivery is kept."""
    envelope = {"id": "evt-1", "data": {"run": {"runId": "run-\ud800", "facets": {"rask_author": {"sub": "al\ud800ice"}}}}}
    feed = Feed()

    with started(monkeypatch, feed) as lineage:
        attempts = get_settings().delivery_attempts
        settled = lineage.deliver(envelope, attempt=attempts + 1)

    assert settled == Settlement(outcome=Outcome.PARKED)
    assert feed.kept == {
        "dead_letter:evt-1": Kept(
            run_id=None, author=None, reason=f"every one of its {attempts} deliveries ended before its handling did", event_json=json.dumps(envelope)
        )
    }


@pytest.mark.parametrize(
    ("unreadable", "refusals_refused"),
    [
        pytest.param(True, False, id="the-feed-cannot-be-asked"),
        pytest.param(False, True, id="the-refusals-table-refuses-the-row"),
    ],
)
def test_a_park_the_store_does_not_take_is_asked_for_again(monkeypatch: pytest.MonkeyPatch, counts: Counts, unreadable: bool, refusals_refused: bool) -> None:
    feed = Feed(unrecordable=True, unreadable=unreadable, refusals_refused=refusals_refused)

    settled, _ = _parked_on_its_last_delivery(monkeypatch, feed)

    assert settled == Settlement(outcome=Outcome.AGAIN, delay_seconds=get_settings().delivery_retry_seconds)
    assert feed.kept == {}
    assert counts("lineage.events.processed") == {outcome("retried", "subscriber"): 1}
