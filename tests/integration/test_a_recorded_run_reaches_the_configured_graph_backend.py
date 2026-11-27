"""A pipeline's run, posted with its own token, reaches an event reader and the configured graph backend (LH-468, ADR 0158).

A client that tells people about runs hears of one posted over HTTP only through the feed's projection, which it follows
forward (the HTTP door records; it publishes nothing on the bus), so the run must be on `GET /events/projection` with
the author the token verified. The lineage graph is bring-your-own: the relay tick forwards every event lineage
recorded, as standard OpenLineage, to the backend `LINEAGE_FORWARD_URL` names, over the OpenLineage HTTP transport.
Delivery is at least once and in feed order: a backend that is down holds the forwarder's cursor where it was, and
the next tick sends the event once the backend answers. The cursor is kept in the catalog, so a tick after a delivery
sends nothing again. The feed's retention waits for the forwarder: a row older than the retention is kept while the
backend has not taken it, and goes on the tick that delivered it.

Driven over real sockets: lineage's router behind its real authentication gate records into a live catalog, its
relay tick runs on the app's own event loop as its timer runs it, and the backend is a live OpenLineage endpoint that
records what it is sent.
"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self, override

import httpx
import pytest

from lineage.services.provenance import ProvenanceStore
from tests.app_state import app_of
from tests.integration.lineage_app import (
    SUBJECT_HEADER,
    GraphBackend,
    declared,
    ensure_tables,
    governed_lineage,
    graph_backend,
    live_catalog,
    relay_tick,
    run_event,
)


_TABLE = "lance_catalog$acme-cleaned$pages"
_ALICE = "dex~alice"
#: A client holding `event_reader`, which reads the event feed under its own service account.
_READER = "kubernetes~system:serviceaccount:rask:rask-sa-a-notifier"


class _EightDaysAgo(datetime):
    """The clock lineage stamps a row's arrival with, read past the feed's default retention of seven days."""

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp((datetime.now(UTC) - timedelta(days=8)).timestamp(), tz or UTC)


@pytest.fixture
def backend() -> Generator[tuple[GraphBackend, str]]:
    with graph_backend() as served:
        yield served


def test_a_run_reaches_the_backend_once_across_an_outage_longer_than_the_feeds_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: tuple[GraphBackend, str]
) -> None:
    graph, url = backend
    monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
    run = run_event("1d3c5a07-0000-4000-8000-000000000592", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=[_TABLE])
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        granted = {(_ALICE, "can_write_data", f"table:{declared(catalog_url, _TABLE)[_TABLE]}"), (_READER, "can_read_event_feed", "estate:rask")}
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
            with monkeypatch.context() as clock:
                clock.setattr("lineage.services.provenance.datetime", _EightDaysAgo)
                posted = lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: _ALICE})
            projected = lineage.get("/events/projection", params={"since": 0}, headers={SUBJECT_HEADER: _READER}).json()
            graph.down = True
            while_down = relay_tick(lineage)
            graph.down = False
            once_up = relay_tick(lineage)
            after = relay_tick(lineage)

    assert posted.status_code == 201, posted.text
    assert [(row["event"]["run"]["runId"], row["event"]["run"]["facets"]["rask_author"]["sub"]) for row in projected["events"]] == [
        (run["run"]["runId"], _ALICE)
    ]
    assert [(tick.forwarded, tick.forward_stalled, tick.pruned_events) for tick in (while_down, once_up, after)] == [
        (0, "HTTP 503", 0),
        (1, None, 1),
        (0, None, 0),
    ]
    assert [(event["run"]["runId"], event["eventType"], event["run"]["facets"]["rask_author"]["sub"]) for event in graph.received] == [
        (run["run"]["runId"], "COMPLETE", _ALICE)
    ]


def _runs(first: int, count: int) -> list[dict[str, Any]]:
    """``count`` completed runs writing the table, numbered from ``first``, in the order they are posted."""
    return [
        run_event(f"1d3c5a07-0000-4000-8000-{n:012d}", "COMPLETE", f"2026-10-15T10:{n // 60:02d}:{n % 60:02d}+00:00", outputs=[_TABLE])
        for n in range(first, first + count)
    ]


def _feed_created_again(catalog_url: str, store: ProvenanceStore) -> None:
    """Replace the feed and keep the forwarder's cursor: the feed table dropped at the catalog, then created again as
    lineage's start creates a table its store lacks. The ``cursors`` table is untouched."""
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        dropped = catalog.post("/v1/table/lance_catalog$provenance$events/drop", json={})
    assert dropped.status_code == 200, dropped.text
    ensure_tables(store)


def test_a_feed_created_again_is_forwarded_from_its_first_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: tuple[GraphBackend, str]) -> None:
    """A cursor is a position in the feed it was taken on ([[LH-599]]). A feed table created again numbers its rows from
    the start, so a cursor kept from the feed before it names rows of another table: the forwarder follows each new feed
    from its first row, whether its first tick finds it holding as many rows as the last cursor had passed, more, or
    none yet. On one feed a tick whose cursor is at the newest row sends nothing."""
    graph, url = backend
    monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
    first, as_many, more, refilled = _runs(0, 2), _runs(100, 2), _runs(200, 3), _runs(300, 1)
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        granted = {(_ALICE, "can_write_data", f"table:{declared(catalog_url, _TABLE)[_TABLE]}")}
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:

            def post(runs: list[dict[str, Any]]) -> None:
                for run in runs:
                    posted = lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: _ALICE})
                    assert posted.status_code == 201, posted.text

            def tick() -> list[str]:
                """The runs one relay tick sent the backend, in the order it took them."""
                del graph.received[:]
                relay_tick(lineage)
                return [event["run"]["runId"] for event in graph.received]

            def on_a_new_feed(runs: list[dict[str, Any]]) -> list[str]:
                """What the first tick on a feed created again sends, ``runs`` having been recorded on it before that tick."""
                _feed_created_again(catalog_url, app_of(lineage).state.provenance)
                post(runs)
                return tick()

            post(first)
            sent = [tick(), on_a_new_feed(as_many), on_a_new_feed(more), on_a_new_feed([])]
            post(refilled)
            sent += [tick(), tick()]

    assert sent == [[run["run"]["runId"] for run in runs] for runs in (first, as_many, more, [], refilled, [])]
