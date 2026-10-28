"""An erasure finds every table derived from a subject's source, off the table-level edge list (LH-468, ADR 0158).

Lineage keeps no graph: what derives from a table is answered by walking the edge list its recorded runs left,
a Lance table the catalog keeps. Driven end to end: a pipeline's bronze, silver and gold writes name their runs and
inputs on the catalog's own doors (`x-rask-run-id`, `x-rask-inputs`), the catalog announces each commit over HTTP to
lineage's app served beside it, and lineage records the edges back into that catalog.

An external input never resolves onto a governed table's edges ([[LIN-3]]): a producer naming a source outside the
estate by a governed table's name records an edge from that source, under its namespace-qualified identity, so a
walk from the governed table does not reach what the producer wrote.

An answer that hides a derived table from the caller says it is incomplete: an erasure that cannot see every table
built from its source must not read what it can see as all of them. Each table is seen through the grants on the uuid
the catalog minted for it, ``table:<uuid>`` ([[LH-524]]).

A rename does not split what a table fed ([[LH-522]]): a governed table is recorded by the uuid the catalog minted for
it, which the rename keeps, so the walk from its new name and the feed scoped to it reach what it fed under the old one.

The catalog's erasure verification walks the same edge list ([[LH-332]], ADR 0178): it names every table derived from
the erased one by uuid and holds the obligation open until each has an obligation of its own following it, since the
source's predicate cannot judge another table's columns. It walks the catalog's own record of what each commit declared
it read beside it ([[LH-541]], ADR 0183), so a table written while lineage is down is named all the same; and the commit
reconciler's tick lists on a released obligation a table lineage records only after the release, whose run came before it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from service_kit.openlineage import DatasetUuids, with_dataset_uuids
from tests.integration.lineage_app import SUBJECT_HEADER, free_port, governed_lineage, live_catalog, live_lineage, run_event


#: The commit reconciler's binding name, which its tick is mounted at as the chart names it.
_TICK = "catalog-commit-reconcile-cron"


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}

#: A subject no table holds a row of (`_rows` writes ids 0 to 2), as the erasure job leaves every table it rewrote: the
#: claims below are about what the derived tables hold the obligation to, not about any table's bytes.
_GONE = "id = 99"


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


def _declared(catalog: httpx.Client, *tables: str) -> dict[str, str]:
    """Declare each of ``tables`` (and its namespace) in the catalog, and answer the uuid the catalog minted for each."""
    for namespace in dict.fromkeys(table.split("$")[0] for table in tables):
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
    for table in tables:
        assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
    return {table: catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"]["rask.uuid"] for table in tables}


def _stamped(event: dict[str, object], uuid_of: dict[str, str]) -> dict[str, object]:
    """``event`` with each dataset it names carrying its table's uuid, as the catalog's own events carry it (ADR 0173 §10)."""

    def stamp(datasets: object) -> list[dict[str, object]]:
        assert isinstance(datasets, list)
        return [with_dataset_uuids(d, DatasetUuids(table_uuid=uuid_of[d["name"]]), producer="https://example.invalid/etl") for d in datasets]

    return {**event, "inputs": stamp(event["inputs"]), "outputs": stamp(event["outputs"])}


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


def _obligation(catalog: httpx.Client, table: str, *, follows: str | None = None) -> str:
    """Record an erasure obligation on ``table`` for the subject :data:`_GONE` names, following ``follows``; answer its id."""
    recorded = catalog.post(f"/management/v1/table/{table}/erasure", json={"predicate": _GONE, **({"follows": follows} if follows else {})})
    assert recorded.status_code == 202, recorded.text
    return str(recorded.json()["obligation_id"])


type _Gold = Callable[[httpx.Client, dict[str, str]], None]


def _gold_holds_none(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Nobody has recorded an obligation on gold."""


def _gold_follows_silvers(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Gold's obligation follows silver's, which follows bronze's: gold was derived from bronze through silver."""
    _obligation(catalog, "acme-gold$catalog", follows=obligations["acme-silver$pages"])


def _gold_is_dropped(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Gold is dropped with its bytes, so nothing of it is left to hold the subject or an obligation."""
    dropped = catalog.post("/v1/table/acme-gold$catalog/drop", json={})
    assert dropped.status_code == 200, dropped.text


def _gold_is_deregistered(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Gold is deregistered: no obligation can be recorded on it any more, and its bytes stay where they were."""
    deregistered = catalog.post("/v1/table/acme-gold$catalog/deregister", json={})
    assert deregistered.status_code == 200, deregistered.text


@pytest.mark.parametrize(
    ("arrange", "gold", "state"),
    [
        pytest.param(_gold_holds_none, "failed", "open", id="a-derived-table-no-obligation-follows"),
        pytest.param(_gold_follows_silvers, "followed", "released", id="an-obligation-following-through-a-chain"),
        pytest.param(_gold_is_dropped, "gone", "released", id="a-derived-table-dropped-with-its-bytes"),
        pytest.param(_gold_is_deregistered, "held", "open", id="a-derived-table-deregistered-with-its-bytes"),
    ],
)
def test_an_erasure_is_held_until_every_table_derived_from_its_source_has_an_obligation_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: _Gold, gold: str, state: str
) -> None:
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port),
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
    ):
        for namespace in ("acme-bronze", "acme-silver", "acme-gold"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table, headers in (
            ("acme-bronze$events", _ARROW),
            ("acme-silver$pages", _run("silver-1", "acme-bronze$events")),
            ("acme-gold$catalog", _run("gold-1", "acme-silver$pages")),
        ):
            created = catalog.post(f"/v1/table/{table}/create", content=_rows(), headers=headers)
            assert created.status_code == 200, created.text
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"]["rask.uuid"] for name in ("acme-silver$pages", "acme-gold$catalog")
        }
        source = _obligation(catalog, "acme-bronze$events")
        obligations = {"acme-silver$pages": _obligation(catalog, "acme-silver$pages", follows=source)}
        arrange(catalog, obligations)

        verified = catalog.post(f"/management/v1/table/acme-bronze$events/erasure/{source}/verify", json={"predicate": _GONE})

    assert verified.status_code == 200, verified.text
    report, obligation = verified.json()["report"], verified.json()["obligation"]
    derived = [(surface["surface"], surface["outcome"]) for surface in report["surfaces"] if surface["surface"].startswith("derived")]
    assert derived == [(f"derived:{uuid_of['acme-silver$pages']}", "followed"), (f"derived:{uuid_of['acme-gold$catalog']}", gold)]
    still_open = [f"derived:{uuid_of['acme-gold$catalog']}"] if state == "open" else []
    assert ([surface["surface"] for surface in obligation["open_surfaces"]], obligation["state"]) == (still_open, state)


def _silver_created(catalog: httpx.Client, url: str) -> None:
    created = catalog.post("/v1/table/acme-silver$pages/create", content=_rows(), headers=_run("silver-1", "acme-bronze$events"))
    assert created.status_code == 200, created.text


def _silver_inserted(catalog: httpx.Client, url: str) -> None:
    assert catalog.post("/v1/table/acme-silver$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    inserted = catalog.post("/v1/table/acme-silver$pages/insert", content=_rows(), headers=_run("silver-1", "acme-bronze$events"))
    assert inserted.status_code == 200, inserted.text


def _silver_merged(catalog: httpx.Client, url: str) -> None:
    assert catalog.post("/v1/table/acme-silver$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    merged = catalog.post(
        "/v1/table/acme-silver$pages/merge_insert",
        params={"on": "id", "when_matched_update_all": "true", "when_not_matched_insert_all": "true"},
        content=_rows(),
        headers=_run("silver-1", "acme-bronze$events"),
    )
    assert merged.status_code == 200, merged.text


def _silver_appended_by_a_stock_client(catalog: httpx.Client, url: str) -> None:
    """Stock pylance on the catalog's namespace doors, committing through ``version/create`` with its run in the
    transaction properties (ADR 0157)."""
    assert catalog.post("/v1/table/acme-silver$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    inputs = json.dumps([{"table": ["acme-bronze", "events"], "version": 1}])
    lance.write_dataset(
        pa.table({"id": pa.array([7], pa.int64())}),
        namespace_client=lance_namespace.connect("rest", {"uri": url}),
        table_id=["acme-silver", "pages"],
        mode="append",
        transaction_properties={"rask.run_id": "silver-1", "rask.inputs": inputs},
    )


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(_silver_created, id="create"),
        pytest.param(_silver_inserted, id="insert"),
        pytest.param(_silver_merged, id="merge-insert"),
        pytest.param(_silver_appended_by_a_stock_client, id="version-create"),
    ],
)
def test_a_table_written_from_the_source_while_lineage_is_down_holds_the_erasure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[httpx.Client, str], None]
) -> None:
    """Every announcement goes to a port nothing listens on, so lineage records no edge: the verification names silver from
    what the catalog recorded when silver's write declared bronze, and the obligation stays open."""
    down = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{free_port()}/api/v1/lineage"}
    with live_catalog(tmp_path / "catalog", monkeypatch, **down) as catalog_url, httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        for namespace in ("acme-bronze", "acme-silver"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post("/v1/table/acme-bronze$events/create", content=_rows(), headers=_ARROW).status_code == 200
        write(catalog, catalog_url)
        silver = catalog.post("/v1/table/acme-silver$pages/describe", json={}).json()["properties"]["rask.uuid"]
        source = _obligation(catalog, "acme-bronze$events")

        verified = catalog.post(f"/management/v1/table/acme-bronze$events/erasure/{source}/verify", json={"predicate": _GONE})

    assert verified.status_code == 200, verified.text
    report, obligation = verified.json()["report"], verified.json()["obligation"]
    assert [(surface["surface"], surface["outcome"]) for surface in report["surfaces"] if surface["surface"].startswith("derived")] == [
        (f"derived:{silver}", "failed")
    ]
    assert ([surface["surface"] for surface in obligation["open_surfaces"]], obligation["state"]) == ([f"derived:{silver}"], "open")
    assert "A write that declared no inputs derives from nothing" in report["derived_scope"]


class _Counter:
    """The paging counter's instrument, recording what it is handed."""

    def __init__(self) -> None:
        self.added: list[int] = []

    def add(self, amount: int, attributes: Mapping[str, str] | None = None, context: object = None) -> None:
        self.added.append(amount)


def test_a_table_lineage_records_after_the_release_from_a_run_before_it_is_listed_on_the_obligation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An outside producer's run wrote ``acme-silver$late`` from bronze before the release and lineage records it after: the
    reconciler's tick lists it on the released obligation once, and pages."""
    from catalog.api import commit_reconcile

    counted = _Counter()
    monkeypatch.setattr(commit_reconcile, "_late_derived", counted)
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        from catalog.main import app

        # Mounted as the chart's binding name mounts it at boot (`main.py`), on a copy of the routes the test gives back.
        monkeypatch.setattr(app.router, "routes", list(app.router.routes))
        commit_reconcile.mount_commit_reconcile(app, _TICK)
        assert catalog.post("/v1/namespace/acme-bronze/create", json={}).status_code == 200
        assert catalog.post("/v1/table/acme-bronze$events/create", content=_rows(), headers=_ARROW).status_code == 200
        uuid_of = {"acme-bronze$events": catalog.post("/v1/table/acme-bronze$events/describe", json={}).json()["properties"]["rask.uuid"]}
        uuid_of |= _declared(catalog, "acme-silver$late")
        source = _obligation(catalog, "acme-bronze$events")
        released = catalog.post(f"/management/v1/table/acme-bronze$events/erasure/{source}/verify", json={"predicate": _GONE})
        assert released.json()["obligation"]["state"] == "released", released.text
        run_at = datetime.fromisoformat(released.json()["obligation"]["released_at"]) - timedelta(minutes=1)
        outside = run_event(str(uuid.uuid4()), "COMPLETE", run_at.isoformat(), outputs=["acme-silver$late"], inputs=("acme-bronze$events",))
        assert lineage.post("/api/v1/lineage", json=_stamped(outside, uuid_of)).status_code == 201

        ticks = [catalog.post(f"/{_TICK}").json()["late_derived"] for _ in range(2)]
        listed = catalog.get("/management/v1/erasures").json()["obligations"]

    assert (ticks, counted.added) == ([1, 0], [1])
    assert [(o["obligation_id"], o["late_derived"]) for o in listed] == [(source, [uuid_of["acme-silver$late"]])]


def test_an_answer_that_hides_a_derived_table_says_it_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.models import parse_event

    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
            uuid_of = _declared(catalog, "acme-bronze$events", "acme-silver$pages", "acme-gold$catalog")
        granted = {("dex~eraser", "can_get_metadata", f"table:{uuid_of[name]}") for name in ("acme-bronze$events", "acme-silver$pages")}
        chain = [
            run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:01+00:00", outputs=["acme-silver$pages"], inputs=("acme-bronze$events",)),
            run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:02+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$pages",)),
        ]
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
            for event in chain:
                asyncio.run(lineage.app.state.provenance.record(parse_event(_stamped(event, uuid_of))))

            answer = lineage.get("/tables/acme-bronze$events/derived", headers={SUBJECT_HEADER: "dex~eraser"})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "acme-bronze$events",
        "uuid": uuid_of["acme-bronze$events"],
        "derived": [{"table": "acme-silver$pages", "uuid": uuid_of["acme-silver$pages"], "depth": 1}],
        "complete": False,
    }


def test_a_renamed_source_answers_what_it_fed_under_both_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read through lineage's FGA gate by a caller granted on the tables' uuids, which a rename keeps and so moves no
    grant: the write recorded under the old name is answered as the table's."""
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
        granted = {("dex~owner", "can_get_metadata", f"table:{table_uuid}") for table_uuid in uuid_of.values()}
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
            derived = lineage.get("/tables/acme-bronze$u/derived", headers={SUBJECT_HEADER: "dex~owner"})
            scoped = lineage.get("/events", params={"table": "acme-bronze$u", "since": 0}, headers={SUBJECT_HEADER: "dex~owner"})

    assert derived.status_code == 200, derived.text
    assert derived.json() == {
        "table": "acme-bronze$u",
        "uuid": uuid_of["acme-bronze$u"],
        "derived": [
            {"table": "acme-silver$x", "uuid": uuid_of["acme-silver$x"], "depth": 1},
            {"table": "acme-silver$y", "uuid": uuid_of["acme-silver$y"], "depth": 1},
        ],
        "complete": True,
    }
    assert scoped.status_code == 200, scoped.text
    silver = [row for row in scoped.json()["events"] if row["outputs"][0].startswith("acme-silver$")]
    writes = {(tuple(row["inputs"]), tuple(row["outputs"])) for row in silver}
    assert writes == {
        (("acme-bronze$t",), ("acme-silver$x",)),
        (("acme-bronze$u",), ("acme-silver$y",)),
    }, scoped.json()["events"]
