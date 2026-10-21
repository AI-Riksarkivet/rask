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

A rename does not split what a table fed ([[LH-522]]): a governed table is recorded by the uuid the catalog minted for
it, which the rename keeps, so the walk from its new name and the feed scoped to it reach what it fed under the old one.
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
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"]["rask.uuid"]
            for name in ("acme-bronze$events", "acme-silver$pages", "acme-gold$catalog")
        }

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "acme-bronze$events",
        "uuid": uuid_of["acme-bronze$events"],
        "derived": [
            {"table": "acme-silver$pages", "uuid": uuid_of["acme-silver$pages"], "depth": 1},
            {"table": "acme-gold$catalog", "uuid": uuid_of["acme-gold$catalog"], "depth": 2},
        ],
        "complete": True,
    }


def test_an_answer_that_hides_a_derived_table_says_it_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.models import parse_event

    granted = {("dex~eraser", "can_get_metadata", f"table:{name}") for name in ("acme-bronze$events", "acme-silver$pages")}
    chain = [
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:01+00:00", outputs=["acme-silver$pages"], inputs=("acme-bronze$events",)),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:02+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$pages",)),
    ]
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, granted) as lineage:
        for event in chain:
            asyncio.run(lineage.app.state.provenance.record(parse_event(event)))

        answer = lineage.get("/tables/acme-bronze$events/derived", headers={SUBJECT_HEADER: "dex~eraser"})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "acme-bronze$events",
        "uuid": None,
        "derived": [{"table": "acme-silver$pages", "uuid": None, "depth": 1}],
        "complete": False,
    }


def test_a_renamed_source_answers_what_it_fed_under_both_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read through lineage's FGA gate by a caller granted on the tables' names now, as a rename leaves the grants: the
    write recorded under the old name is answered as the table's, and so is an outside producer's run its credential
    could not resolve, which is recorded by the name."""
    from lineage.models import parse_event

    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url, httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        with live_lineage(catalog_url, monkeypatch, port=port):
            for namespace in ("acme-bronze", "acme-silver"):
                assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
            assert catalog.post("/v1/table/acme-bronze$t/create", content=_rows(), headers=_ARROW).status_code == 200
            assert catalog.post("/v1/table/acme-silver$x/create", content=_rows(), headers=_run("x-1", "acme-bronze$t")).status_code == 200
            assert catalog.post("/v1/table/acme-bronze$t/rename", json={"new_table_name": "u"}).status_code == 200
            assert catalog.post("/v1/table/acme-silver$y/create", content=_rows(), headers=_run("y-1", "acme-bronze$u")).status_code == 200
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"]["rask.uuid"]
            for name in ("acme-bronze$u", "acme-silver$x", "acme-silver$y")
        }
        granted = {("dex~owner", "can_get_metadata", f"table:{name}") for name in ("acme-bronze$u", "acme-silver$x", "acme-silver$y", "acme-silver$z")}
        unresolved = run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-21T10:00:00+00:00", outputs=["acme-silver$z"], inputs=("acme-bronze$u",))
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
            asyncio.run(lineage.app.state.provenance.record(parse_event(unresolved)))
            derived = lineage.get("/tables/acme-bronze$u/derived", headers={SUBJECT_HEADER: "dex~owner"})
            scoped = lineage.get("/events", params={"table": "acme-bronze$u", "since": 0}, headers={SUBJECT_HEADER: "dex~owner"})

    assert derived.status_code == 200, derived.text
    assert derived.json() == {
        "table": "acme-bronze$u",
        "uuid": uuid_of["acme-bronze$u"],
        "derived": [
            {"table": "acme-silver$x", "uuid": uuid_of["acme-silver$x"], "depth": 1},
            {"table": "acme-silver$y", "uuid": uuid_of["acme-silver$y"], "depth": 1},
            {"table": "acme-silver$z", "uuid": None, "depth": 1},
        ],
        "complete": True,
    }
    assert scoped.status_code == 200, scoped.text
    silver = [row for row in scoped.json()["events"] if row["outputs"][0].startswith("acme-silver$")]
    writes = {(tuple(row["inputs"]), tuple(row["outputs"])) for row in silver}
    assert writes == {
        (("acme-bronze$t",), ("acme-silver$x",)),
        (("acme-bronze$u",), ("acme-silver$y",)),
        (("acme-bronze$u",), ("acme-silver$z",)),
    }, scoped.json()["events"]
