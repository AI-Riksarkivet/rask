"""An erasure finds every table derived from a subject's source, off the table-level edge list (LH-468, ADR 0158).

Lineage keeps no graph: what derives from a table is answered by walking the edge list its recorded runs left,
a Lance table the catalog keeps. Driven end to end: a pipeline's bronze, silver and gold writes name their runs and
inputs on the catalog's own doors (`x-rask-run-id`, `x-rask-inputs`), the catalog announces each commit over HTTP to
lineage's app served beside it, and lineage records the edges back into that catalog.

An external input never resolves onto a governed table's edges ([[LIN-3]]): a producer naming a source outside the
estate by a governed table's name records an edge from that source, under its namespace-qualified identity, so a
walk from the governed table does not reach what the producer wrote.

An answer that hides a derived table from the caller says it is incomplete: an erasure that cannot see every table
built from its source must not read what it can see as all of them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from tests.integration.lineage_app import SUBJECT_HEADER, free_port, governed_lineage, live_catalog, live_lineage, run_event


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _rows() -> bytes:
    table = pa.table({"id": pa.array(range(3), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _run(run_id: str, *inputs: str) -> dict[str, str]:
    """The Context a pipeline's write names its run in: each input read at version 1."""
    named = [{"table": name.split("$"), "version": 1} for name in inputs]
    return {**_ARROW, "x-rask-run-id": run_id, "x-rask-job": "daily", **({"x-rask-inputs": json.dumps(named)} if named else {})}


def test_a_walk_from_a_source_reaches_every_table_built_from_it_and_nothing_an_outside_input_fed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        for namespace in ("acme-bronze", "acme-silver", "acme-gold"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table, headers in (
            ("acme-bronze$events", _ARROW),
            ("acme-silver$pages", _run("silver-1", "acme-bronze$events")),
            ("acme-gold$catalog", _run("gold-1", "acme-silver$pages")),
            ("acme-silver$other", _run("other-1")),
        ):
            created = catalog.post(f"/v1/table/{table}/create", content=_rows(), headers=headers)
            assert created.status_code == 200, created.text
        outside = run_event("7f1d6c1e-0000-4000-8000-000000000501", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=["acme-silver$borrowed"])
        outside["inputs"] = [{"namespace": "s3://elsewhere", "name": "acme-bronze$events"}]
        assert lineage.post("/api/v1/lineage", json=outside).status_code == 201

        answer = lineage.get("/tables/acme-bronze$events/derived")

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "acme-bronze$events",
        "derived": [{"table": "acme-silver$pages", "depth": 1}, {"table": "acme-gold$catalog", "depth": 2}],
        "complete": True,
    }


def test_an_answer_that_hides_a_derived_table_says_it_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.models import parse_event

    granted = {("eraser", "can_get_metadata", f"table:{name}") for name in ("acme-bronze$events", "acme-silver$pages")}
    chain = [
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:01+00:00", outputs=["acme-silver$pages"], inputs=("acme-bronze$events",)),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:02+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$pages",)),
    ]
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, granted) as lineage:
        for event in chain:
            asyncio.run(lineage.app.state.provenance.record(parse_event(event)))

        answer = lineage.get("/tables/acme-bronze$events/derived", headers={SUBJECT_HEADER: "eraser"})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"table": "acme-bronze$events", "derived": [{"table": "acme-silver$pages", "depth": 1}], "complete": False}
