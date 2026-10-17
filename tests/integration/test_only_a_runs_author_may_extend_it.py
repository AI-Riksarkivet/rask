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
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.lineage_app import SUBJECT_HEADER, governed_lineage, live_catalog, run_event


_TABLE = "acme-silver$pages"
_ALICE = "dex~alice"
_BOB = "dex~bob"


@pytest.fixture
def lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    granted = {(who, relation, f"table:{_TABLE}") for who in (_ALICE, _BOB) for relation in ("can_write_data", "can_get_metadata")}
    with live_catalog(tmp_path / "catalog", monkeypatch) as url, governed_lineage(url, monkeypatch, granted) as client:
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
