"""Every catalog write must stamp the physical Lance URI, and none of them did.

`build_write_event` takes `source_uri` and turns it into the standard OpenLineage `dataSource` facet.
`emit_write_event` accepts it. `EmitFields` declares it. The whole channel is plumbed — and
`emit_measured_write`, the trailer EVERY catalog write door goes through (insert, merge_insert, update,
delete, add/alter/drop columns, restore, compact_table), never passed one. So the facet was reachable
from no door, exactly the shape `test_originator_reaches_the_event.py` pins for `lance.originator`.

The cost is already written down at the facet's own declaration
(`lineage_emit.py`, `_DATASOURCE_FACET_SCHEMA`): "Without it, reconcile has no URI to read → every real
table looks `missing_on_storage` (the moat was broken)." That is #23 reconcile mis-reporting live
tables as absent from storage, on every write op the catalog serves.

It is also what the event-driven maintenance lane needs. `services/maintenance` has NO catalog client
BY DESIGN (`maintenance_policies.py`: "read directly off the bucket by the compaction service on every
sweep tick"), so an event naming only a table id gives it nothing it can open — it would have to walk
the buckets to resolve the id, which is the walk the event lane exists to replace.

The URI costs nothing to include: `read_version_and_schema` already opens the dataset to read the
version and the schema in ONE open, and the location is an attribute of that same handle.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from catalog.core.lineage_emit import build_write_event, emit_write_event


class _Recording:
    """Structural double for `LineageEmitter` — the estate's fake-by-shape pattern."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def test_the_channel_reaches_the_facet_when_a_door_supplies_a_uri() -> None:
    """The plumbing below `emit_measured_write` is sound — proving the defect is the CALLER, not the
    builder, so the fix belongs at the door and not in the event shape."""
    event = build_write_event(
        table_id="db$t",
        namespace="rask",
        author=None,
        version=3,
        operation="insert",
        run_id="r-1",
        event_time="2026-09-03T00:00:00+00:00",
        job_namespace="catalog",
        source_uri="s3://bucket/abc12345_db$t",
    )
    assert event["outputs"][0]["facets"]["dataSource"]["uri"] == "s3://bucket/abc12345_db$t"


def test_emit_write_event_forwards_a_supplied_uri() -> None:
    recorder = _Recording()
    asyncio.run(
        emit_write_event(
            recorder,
            ["db", "t"],
            delimiter="$",
            author=None,
            version=3,
            operation="insert",
            authorization=None,
            source_uri="s3://bucket/abc12345_db$t",
        )
    )
    assert recorder.writes and recorder.writes[0].get("source_uri") == "s3://bucket/abc12345_db$t"


def test_the_measured_write_trailer_STAMPS_the_uri_it_read_back() -> None:
    """The door every write goes through must stamp the URI, and it must DERIVE it rather than be told.

    Taking it as a parameter would put the burden on each of the dozen call sites and let any one of
    them forget — which is the failure this fixes, one level up. `read_version_and_schema` already
    opens the dataset for version and schema in ONE open, so the location comes off that same handle
    and cannot disagree with the version it is stamped beside.
    """
    import types

    from catalog.api import lineage_deps

    recorder = _Recording()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            lineage_deps.dataplane,
            "read_version_and_schema",
            lambda *a, **k: (4, [], "s3://bucket/abc12345_db$t"),
        )
        asyncio.run(
            lineage_deps.emit_measured_write(
                cast(Any, recorder),
                ["db", "t"],
                ns=cast(Any, None),
                so={},
                settings=cast(Any, types.SimpleNamespace(delimiter="$")),
                token=None,
                operation="insert",
                authorization=None,
            )
        )
    finally:
        monkey.undo()

    assert recorder.writes, "the trailer emitted nothing"
    assert recorder.writes[0].get("source_uri") == "s3://bucket/abc12345_db$t"


def test_a_readback_that_could_not_open_the_dataset_stamps_no_uri() -> None:
    """Degrading to no facet is correct; inventing a URI is not.

    The write is already committed when this runs, so a failed readback must weaken the lineage
    enrichment rather than fail the request — and a `dataSource` facet pointing at a path nobody
    confirmed would send #23 reconcile to the wrong object.
    """
    import types

    from catalog.api import lineage_deps

    recorder = _Recording()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(lineage_deps.dataplane, "read_version_and_schema", lambda *a, **k: (None, [], None))
        asyncio.run(
            lineage_deps.emit_measured_write(
                cast(Any, recorder),
                ["db", "t"],
                ns=cast(Any, None),
                so={},
                settings=cast(Any, types.SimpleNamespace(delimiter="$")),
                token=None,
                operation="insert",
                authorization=None,
            )
        )
    finally:
        monkey.undo()

    assert recorder.writes and recorder.writes[0].get("source_uri") is None
