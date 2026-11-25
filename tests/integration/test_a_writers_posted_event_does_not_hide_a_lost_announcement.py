"""A commit is announced on the feed only by an event the catalog signed ([[LH-576]]).

The commit reconciler re-announces each version the feed holds no announcement of (ADR 0177, [[LH-497]]). Lineage's HTTP
door records an event any writer of a table may post, naming any version of it, and drops the signature from what it
records; only its bus and outbox doors record one, after verifying it. So a posted event naming a version must not read
as the catalog's announcement of that version, or a lost one is never announced. On a real Lance feed laid out as lineage
lays out its feed (`lineage.services.provenance.FEED_SCHEMA`), so the catalog's read is held to the schema lineage writes.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from catalog.services import commit_reconcile
from catalog.services.commit_reconcile import FeedReading
from lakehouse.records import commit_records
from lakehouse.storage.options import StorageOptions
from lineage.services.provenance import FEED_SCHEMA


TABLE = "0b7f6a0e-6a52-4c7a-9a64-3f2d6a1c9e10"
OTHER = "5d0c1f1e-2b7a-4c55-8a0e-9f3b6d2a7c41"
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
MAIN = commit_records.ref_key(None, None)
CATALOG = "rask-catalog"


def _event(version: int, *, signed_by: str | None, table: str = TABLE) -> str:
    """A run announcing ``version`` of ``table``, as lineage records it: signed when it came over the bus."""
    facets = {"rask_signature": {"identity": signed_by}} if signed_by is not None else {}
    output = {
        "namespace": "lance-namespace://catalog",
        "name": "db$t",
        "facets": {"rask_lance": {"tableUuid": table}, "version": {"datasetVersion": str(version)}},
    }
    return json.dumps({"eventType": "COMPLETE", "run": {"runId": f"run-{version}", "facets": facets}, "outputs": [output]})


@pytest.mark.parametrize(
    ("signer", "commits"),
    [
        pytest.param(CATALOG, {1}, id="a-catalog-that-signs-counts-only-its-own"),
        pytest.param(None, {1, 2}, id="a-catalog-that-signs-nothing-counts-every-row"),
    ],
)
def test_a_version_a_writer_posted_is_not_taken_for_the_catalogs_announcement(tmp_path: Path, signer: str | None, commits: set[int]) -> None:
    feed = str(tmp_path / "events")
    now = datetime.now(UTC)
    rows = [
        {"event_key": f"run-{version}@COMPLETE", "event": _event(version, signed_by=signed_by), "table_uuids": [TABLE], "received_at": now}
        for version, signed_by in ((1, CATALOG), (2, None))
    ]
    lance.write_dataset(pa.Table.from_pylist(rows, schema=FEED_SCHEMA), feed)

    reading = FeedReading(feed, StorageOptions(), since=EPOCH, signer=signer)

    assert {version for version in (1, 2) if reading.holds(TABLE, MAIN, version)} == commits


def _row(table: str, version: int) -> dict[str, object]:
    return {
        "event_key": f"{table}-{version}@COMPLETE",
        "event": _event(version, signed_by=CATALOG, table=table),
        "table_uuids": [table],
        "received_at": datetime.now(UTC),
    }


def _feed_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every open of a dataset the reconciler makes from here on, by location."""
    opened: list[str] = []
    open_dataset = commit_reconcile.open_dataset

    def counted(location: str, *, session: lance.Session, storage_options: dict[str, str] | None = None) -> lance.LanceDataset:
        opened.append(location)
        return open_dataset(location, session=session, storage_options=storage_options)

    monkeypatch.setattr(commit_reconcile, "open_dataset", counted)
    return opened


def test_one_reading_of_the_feed_answers_for_every_table_it_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[XC-204]]: a tick asks one reading about all its tables, where a read per table cost the feed's every fragment each."""
    feed = str(tmp_path / "events")
    lance.write_dataset(pa.Table.from_pylist([_row(TABLE, 1), _row(OTHER, 7)], schema=FEED_SCHEMA), feed)
    opened = _feed_opens(monkeypatch)
    reading = FeedReading(feed, StorageOptions(), since=EPOCH, signer=CATALOG)

    held = (reading.holds(TABLE, MAIN, 1), reading.holds(OTHER, MAIN, 7))

    assert (held, opened) == ((True, True), [feed])


def test_a_row_that_reached_the_feed_after_the_ticks_reading_is_found_before_its_commit_is_called_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick reads its tables one after another, so a later table's announcement can land after the tick's one reading
    and inside the grace that table is judged by: announcing it again would page for a loss that never happened."""
    feed = str(tmp_path / "events")
    lance.write_dataset(pa.Table.from_pylist([_row(OTHER, 7)], schema=FEED_SCHEMA), feed)
    reading = FeedReading(feed, StorageOptions(), since=EPOCH, signer=CATALOG)
    assert reading.holds(OTHER, MAIN, 7)
    lance.write_dataset(pa.Table.from_pylist([_row(TABLE, 2)], schema=FEED_SCHEMA), feed, mode="append")
    opened = _feed_opens(monkeypatch)

    held = (reading.holds(TABLE, MAIN, 2), reading.holds(TABLE, MAIN, 3), reading.holds(TABLE, MAIN, 4))

    # Found on the table's own read; a commit the feed lacks costs that one read however many of its versions are asked.
    assert (held, opened) == ((True, False, False), [feed])
