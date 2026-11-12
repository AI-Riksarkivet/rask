"""A commit is announced on the feed only by an event the catalog signed ([[LH-576]]).

The commit reconciler re-announces each version the feed holds no announcement of (ADR 0177, [[LH-497]]). Lineage's HTTP
door records an event any writer of a table may post, naming any version of it, and drops the signature from what it
records; only its bus and outbox doors record one, after verifying it. So a posted event naming a version must not read
as the catalog's announcement of that version, or a lost one is never announced. On a real Lance feed laid out as lineage
lays out its feed (`lineage.services.provenance.FEED_SCHEMA`), so the catalog's read is held to the schema lineage writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from catalog.services.commit_reconcile import announced
from lakehouse import commit_records
from lakehouse.storage_options import StorageOptions
from lineage.services.provenance import FEED_SCHEMA


TABLE = "0b7f6a0e-6a52-4c7a-9a64-3f2d6a1c9e10"
CATALOG = "rask-catalog"


def _event(version: int, *, signed_by: str | None) -> str:
    """A run announcing ``version`` of the table, as lineage records it: signed when it came over the bus."""
    facets = {"rask_signature": {"identity": signed_by}} if signed_by is not None else {}
    output = {
        "namespace": "lance-namespace://catalog",
        "name": "db$t",
        "facets": {"rask_lance": {"tableUuid": TABLE}, "version": {"datasetVersion": str(version)}},
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

    on_feed = announced(feed, StorageOptions(), TABLE, since=datetime(2000, 1, 1, tzinfo=UTC), signer=signer)

    assert on_feed.commits == {(commit_records.ref_key(None, None), version) for version in commits}
