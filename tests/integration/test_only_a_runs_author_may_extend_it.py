"""Only a run's author may extend it: an event naming a recorded run is refused from anyone else ([[LIN-5]], LH-468).

Run ids are public — every feed row serves one — and an event naming an existing run is recorded as that run's
next state. So an authenticated caller who could name any run could set its state, its operation or its error
for every reader of the feed, attributed to a run its author made. Authorizing the extension by the data the run
wrote did not close it: a run that recorded no output (a START naming only an external prefix, an output-less
terminal event) had nothing to authorize against, and anyone extended it; and a run that did record outputs
could be extended by anyone else who may write them.

The rule is identity: the verified subject of the run's first recorded event is its author, and only that
subject records another event of it. The binding outlives the feed's retention, because the run id stays public in
every graph backend the feed was forwarded to: once the feed's rows of a run are pruned, the run is still its
author's. Driven against lineage's real router and gate over the real store in the catalog (`lineage_app`).

A static change (a `DatasetEvent`) has no run to bind, and its identity is derived from what it names, all of which a
writer of the table can read: its producer, its dataset, its operation and its commit's time. So the catalog's own
announcement of a change and an event a poster sends under the same identity are kept as two rows: a row the catalog's
signed announcement needs is never one a poster could take first.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from catalog.core.lineage_emit import build_write_event
from lakehouse.lineage_events import DatasetUuids
from lakehouse.openlineage import DEFAULT_DATASET_NAMESPACE
from lineage.api.v1.endpoints.events import targeting_view
from lineage.core.metrics import Door
from lineage.services.consumer import handle_cloud_event
from tests.integration.lineage_app import SUBJECT_HEADER, governed_lineage, live_catalog, run_event


_TABLE = "lance_catalog$acme-silver$pages"
_ALICE = "dex~alice"
_BOB = "dex~bob"


def _declared(catalog_url: str, table: str) -> str:
    """Declare ``table`` in the catalog at ``catalog_url`` and answer the uuid it minted, the object its grants are held by.

    Lineage's HTTP door resolves each name an event names to that uuid as the caller, and checks the grants on it
    ([[LH-524]]), so the table must exist for any event naming it to be admitted.
    """
    namespace = table.rsplit("$", 1)[0]
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"])


#: The catalog's signing identity, which signs its announcements for the person it authenticated.
_CATALOG = "service-catalog"


@pytest.fixture
def lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        obj = f"table:{_declared(url, _TABLE)}"
        granted = {(who, relation, obj) for who in (_ALICE, _BOB) for relation in ("can_write_data", "can_get_metadata")}
        with governed_lineage(url, monkeypatch, granted) as client:
            yield client


def test_a_run_is_extended_by_its_author_and_refused_to_anyone_else(lineage: TestClient) -> None:
    run = str(uuid.uuid4())

    opened = lineage.post("/api/v1/lineage", json=run_event(run, "START", "2026-10-15T10:00:00+00:00", outputs=[]), headers={SUBJECT_HEADER: _ALICE})
    # Retention takes every row the feed holds, the run's START among them.
    asyncio.run(lineage.app.state.provenance.prune_events(0))
    hijacked = lineage.post("/api/v1/lineage", json=run_event(run, "COMPLETE", "2026-10-15T10:05:00+00:00", outputs=[_TABLE]), headers={SUBJECT_HEADER: _BOB})
    finished = lineage.post("/api/v1/lineage", json=run_event(run, "COMPLETE", "2026-10-15T10:01:00+00:00", outputs=[_TABLE]), headers={SUBJECT_HEADER: _ALICE})

    assert opened.status_code == 201, opened.text
    assert hijacked.status_code == 403, hijacked.text
    assert finished.status_code == 201, finished.text
    # WHAT WAS RECORDED, read off the store: the feed door hides a row naming no dataset from every caller.
    recorded = asyncio.run(lineage.app.state.provenance.list_events(since=0))
    assert [(row.event_type, row.author) for row in recorded if row.event["run"]["runId"] == run] == [("COMPLETE", _ALICE)]


def test_a_change_the_catalog_announces_is_recorded_though_a_poster_sent_its_identity_first(lineage: TestClient, event_signer: Any) -> None:
    table_uuid = next(iter(lineage.app.state.grants.allowed))[2].removeprefix("table:")
    announced = build_write_event(
        table_id=_TABLE,
        namespace=DEFAULT_DATASET_NAMESPACE,
        author=_ALICE,
        version=1,
        operation="create_table",
        run_id=str(uuid.uuid4()),
        event_time="2026-11-12T10:00:00.123456+00:00",
        job_namespace="lance",
        uuids=DatasetUuids(table_uuid=table_uuid),
    )

    # Everything the identity is derived from is the announcement's own, and a writer of the table reads each of them.
    posted = lineage.post("/api/v1/lineage", json=announced, headers={SUBJECT_HEADER: _BOB})
    delivered = asyncio.run(
        handle_cloud_event(lineage.app.state.provenance, {"data": event_signer(_CATALOG).sign(announced, on_behalf_of=_ALICE)}, door=Door.SUBSCRIBER)
    )

    assert posted.status_code == 201, posted.text
    assert delivered == {"status": "SUCCESS"}
    recorded = asyncio.run(lineage.app.state.provenance.list_events(since=0))
    assert sorted((row.author, "rask_signature" in row.event["dataset"]["facets"]) for row in recorded if row.job is None) == [(_ALICE, True), (_BOB, False)]
    # A feed walker tells the two apart by the signer lineage verified, which the projection keeps ([[LH-576]]).
    projected = [targeting_view(row.event)["dataset"]["facets"] for row in recorded if row.job is None]
    assert sorted((facets["rask_author"]["sub"], facets.get("rask_signature", {}).get("identity")) for facets in projected) == [
        (_ALICE, _CATALOG),
        (_BOB, None),
    ]
