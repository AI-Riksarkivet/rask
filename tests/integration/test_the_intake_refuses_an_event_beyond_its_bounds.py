"""The OpenLineage intake bounds what one event may cost the store ([[LIN-6]], LH-468).

A successful run records an edge per (input, output) pair, and its feed row stores the whole event, so an event
naming unbounded datasets or carrying an unbounded facet would let one request grow the edge list and the feed
without limit. The intake refuses an event naming more than 64 datasets on either side (the catalog's own bound on a
run's inputs) or weighing more than 1 MiB as JSON (the bus's own ceiling), and records nothing of it. Driven at
lineage's HTTP door, its store a live catalog; every other door parses through the same models.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration.lineage_app import live_catalog, live_lineage, run_event


@pytest.mark.parametrize(
    ("inputs", "outputs", "note_bytes", "status"),
    [
        pytest.param(64, 64, 0, 201, id="at-the-dataset-bounds"),
        pytest.param(65, 1, 0, 422, id="one-input-over"),
        pytest.param(0, 65, 0, 422, id="one-output-over"),
        pytest.param(0, 1, 1024 * 1024, 422, id="over-the-weight"),
    ],
)
def test_an_event_beyond_the_intakes_bounds_is_refused_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inputs: int, outputs: int, note_bytes: int, status: int
) -> None:
    run = str(uuid.uuid4())
    note = {"note": {"_producer": "https://example.invalid/etl", "text": "x" * note_bytes}} if note_bytes else None
    event = run_event(
        run,
        "COMPLETE",
        "2026-10-15T10:00:00+00:00",
        outputs=[f"acme-gold$t{i}" for i in range(outputs)],
        inputs=tuple(f"acme-silver$t{i}" for i in range(inputs)),
        facets=note,
    )
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch) as lineage_url,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        answer = lineage.post("/api/v1/lineage", json=event)
        recorded = [row for row in lineage.get("/events", params={"since": 0}).json()["events"] if row["event"]["run"]["runId"] == run]

    assert (answer.status_code, len(recorded)) == (status, 1 if status == 201 else 0), answer.text
