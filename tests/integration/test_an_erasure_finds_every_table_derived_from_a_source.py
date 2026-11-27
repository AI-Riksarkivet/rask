"""An erasure finds every table derived from a subject's source, off the table-level edge list (LH-468, ADR 0158).

Lineage keeps no graph: what derives from a table is answered by walking the edge list its recorded runs left,
a Lance table the catalog keeps. Driven end to end: a pipeline's raw, cleaned and served writes name their runs and
inputs on the catalog's own doors (`x-rask-run-id`, `x-rask-inputs`), the catalog announces each commit on the bus it shares with
lineage's app served beside it (`lineage_app.joined_bus`), and lineage records the edges back into that catalog.

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

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.naming import UUID_PROPERTY
from tests.app_state import app_of
from tests.integration.lineage_app import SUBJECT_HEADER, catalog_tick, declared, governed_lineage, joined_bus, live_catalog, live_lineage, run_event


_ARROW = {"content-type": ARROW_STREAM_MEDIA_TYPE}

#: A subject no table holds a row of (`_rows` writes ids 0 to 2), as the erasure job leaves every table it rewrote: the
#: claims below are about what the derived tables hold the obligation to, not about any table's bytes.
_GONE = "id = 99"


def _rows() -> bytes:
    table = pa.table({"id": pa.array(range(3), pa.int64())})
    return encode_arrow_stream(table)


def _run(label: str, *inputs: str) -> dict[str, str]:
    """The Context a pipeline's write names its run in, the run's UUID derived from ``label``: each input read at version 1."""
    named = [{"table": name.split("$"), "version": 1} for name in inputs]
    run = {"x-rask-run-id": str(uuid.uuid5(uuid.NAMESPACE_URL, label)), "x-rask-job-namespace": "pipelines", "x-rask-job": "daily"}
    return {**_ARROW, **run, **({"x-rask-inputs": json.dumps(named)} if named else {})}


def _stamped(event: dict[str, object], uuid_of: dict[str, str]) -> dict[str, object]:
    """``event`` with each dataset it names carrying its table's uuid, as the catalog's own events carry it (ADR 0173 §10)."""

    def stamp(datasets: object) -> list[dict[str, object]]:
        assert isinstance(datasets, list)
        return [with_dataset_uuids(d, DatasetUuids(table_uuid=uuid_of[d["name"]]), producer="https://example.invalid/etl") for d in datasets]

    return {**event, "inputs": stamp(event["inputs"]), "outputs": stamp(event["outputs"])}


def test_a_walk_from_a_source_reaches_every_table_built_from_it_and_nothing_an_outside_input_fed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    joined_bus(monkeypatch)
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        for namespace in ("lance_catalog$acme-raw", "lance_catalog$acme-cleaned", "lance_catalog$acme-served"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table, headers in (
            ("lance_catalog$acme-raw$events", _ARROW),
            ("lance_catalog$acme-cleaned$pages", _run("cleaned-1", "lance_catalog$acme-raw$events")),
            ("lance_catalog$acme-served$catalog", _run("served-1", "lance_catalog$acme-cleaned$pages")),
            ("lance_catalog$acme-cleaned$other", _run("other-1")),
        ):
            created = catalog.post(f"/v1/table/{table}/create", content=_rows(), headers=headers)
            assert created.status_code == 200, created.text
        outside = run_event("7f1d6c1e-0000-4000-8000-000000000501", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=["lance_catalog$acme-cleaned$borrowed"])
        outside["inputs"] = [{"namespace": "s3://elsewhere", "name": "lance_catalog$acme-raw$events"}]
        assert lineage.post("/api/v1/lineage", json=outside).status_code == 201

        answer = lineage.get("/tables/lance_catalog$acme-raw$events/derived")
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"][UUID_PROPERTY]
            for name in ("lance_catalog$acme-raw$events", "lance_catalog$acme-cleaned$pages", "lance_catalog$acme-served$catalog")
        }

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "lance_catalog$acme-raw$events",
        "uuid": uuid_of["lance_catalog$acme-raw$events"],
        "derived": [
            {"table": "lance_catalog$acme-cleaned$pages", "uuid": uuid_of["lance_catalog$acme-cleaned$pages"], "depth": 1},
            {"table": "lance_catalog$acme-served$catalog", "uuid": uuid_of["lance_catalog$acme-served$catalog"], "depth": 2},
        ],
        "complete": True,
    }


def _obligation(catalog: httpx.Client, table: str, *, follows: str | None = None) -> str:
    """Record an erasure obligation on ``table`` for the subject :data:`_GONE` names, following ``follows``; answer its id."""
    recorded = catalog.post(f"/management/v1/table/{table}/erasure", json={"predicate": _GONE, **({"follows": follows} if follows else {})})
    assert recorded.status_code == 202, recorded.text
    return str(recorded.json()["obligation_id"])


type _Served = Callable[[httpx.Client, dict[str, str]], None]


def _served_holds_none(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Nobody has recorded an obligation on served."""


def _served_follows_cleaned(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Served's obligation follows cleaned's, which follows raw's: served was derived from raw through cleaned."""
    _obligation(catalog, "lance_catalog$acme-served$catalog", follows=obligations["lance_catalog$acme-cleaned$pages"])


def _served_is_dropped(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Served is dropped with its bytes, so nothing of it is left to hold the subject or an obligation."""
    dropped = catalog.post("/v1/table/lance_catalog$acme-served$catalog/drop", json={})
    assert dropped.status_code == 200, dropped.text


def _served_is_deregistered(catalog: httpx.Client, obligations: dict[str, str]) -> None:
    """Served is deregistered: no obligation can be recorded on it any more, and its bytes stay where they were."""
    deregistered = catalog.post("/v1/table/lance_catalog$acme-served$catalog/deregister", json={})
    assert deregistered.status_code == 200, deregistered.text


@pytest.mark.parametrize(
    ("arrange", "served", "state"),
    [
        pytest.param(_served_holds_none, "failed", "open", id="a-derived-table-no-obligation-follows"),
        pytest.param(_served_follows_cleaned, "followed", "released", id="an-obligation-following-through-a-chain"),
        pytest.param(_served_is_dropped, "gone", "released", id="a-derived-table-dropped-with-its-bytes"),
        pytest.param(_served_is_deregistered, "held", "open", id="a-derived-table-deregistered-with-its-bytes"),
    ],
)
def test_an_erasure_is_held_until_every_table_derived_from_its_source_has_an_obligation_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: _Served, served: str, state: str
) -> None:
    joined_bus(monkeypatch)
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch),
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
    ):
        for namespace in ("lance_catalog$acme-raw", "lance_catalog$acme-cleaned", "lance_catalog$acme-served"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table, headers in (
            ("lance_catalog$acme-raw$events", _ARROW),
            ("lance_catalog$acme-cleaned$pages", _run("cleaned-1", "lance_catalog$acme-raw$events")),
            ("lance_catalog$acme-served$catalog", _run("served-1", "lance_catalog$acme-cleaned$pages")),
        ):
            created = catalog.post(f"/v1/table/{table}/create", content=_rows(), headers=headers)
            assert created.status_code == 200, created.text
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"][UUID_PROPERTY]
            for name in ("lance_catalog$acme-cleaned$pages", "lance_catalog$acme-served$catalog")
        }
        source = _obligation(catalog, "lance_catalog$acme-raw$events")
        obligations = {"lance_catalog$acme-cleaned$pages": _obligation(catalog, "lance_catalog$acme-cleaned$pages", follows=source)}
        arrange(catalog, obligations)

        verified = catalog.post(f"/management/v1/table/lance_catalog$acme-raw$events/erasure/{source}/verify", json={"predicate": _GONE})

    assert verified.status_code == 200, verified.text
    report, obligation = verified.json()["report"], verified.json()["obligation"]
    derived = [(surface["surface"], surface["outcome"]) for surface in report["surfaces"] if surface["surface"].startswith("derived")]
    assert derived == [
        (f"derived:{uuid_of['lance_catalog$acme-cleaned$pages']}", "followed"),
        (f"derived:{uuid_of['lance_catalog$acme-served$catalog']}", served),
    ]
    still_open = [f"derived:{uuid_of['lance_catalog$acme-served$catalog']}"] if state == "open" else []
    assert ([surface["surface"] for surface in obligation["open_surfaces"]], obligation["state"]) == (still_open, state)


def _cleaned_created(catalog: httpx.Client, url: str) -> None:
    created = catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/create", content=_rows(), headers=_run("cleaned-1", "lance_catalog$acme-raw$events"))
    assert created.status_code == 200, created.text


def _cleaned_inserted(catalog: httpx.Client, url: str) -> None:
    assert catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    inserted = catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/insert", content=_rows(), headers=_run("cleaned-1", "lance_catalog$acme-raw$events"))
    assert inserted.status_code == 200, inserted.text


def _cleaned_merged(catalog: httpx.Client, url: str) -> None:
    assert catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    merged = catalog.post(
        "/v1/table/lance_catalog$acme-cleaned$pages/merge_insert",
        params={"on": "id", "when_matched_update_all": "true", "when_not_matched_insert_all": "true"},
        content=_rows(),
        headers=_run("cleaned-1", "lance_catalog$acme-raw$events"),
    )
    assert merged.status_code == 200, merged.text


def _cleaned_appended_by_a_stock_client(catalog: httpx.Client, url: str) -> None:
    """Stock pylance on the catalog's namespace doors, committing through ``version/create`` with its run in the
    transaction properties (ADR 0157)."""
    assert catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/create", content=_rows(), headers=_ARROW).status_code == 200
    inputs = json.dumps([{"table": ["lance_catalog", "acme-raw", "events"], "version": 1}])
    lance.write_dataset(
        pa.table({"id": pa.array([7], pa.int64())}),
        namespace_client=lance_namespace.connect("rest", {"uri": url}),
        table_id=["lance_catalog", "acme-cleaned", "pages"],
        mode="append",
        transaction_properties={
            "rask.run_id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
            "rask.job_namespace": "pipelines",
            "rask.job": "daily",
            "rask.inputs": inputs,
        },
    )


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(_cleaned_created, id="create"),
        pytest.param(_cleaned_inserted, id="insert"),
        pytest.param(_cleaned_merged, id="merge-insert"),
        pytest.param(_cleaned_appended_by_a_stock_client, id="version-create"),
    ],
)
def test_a_table_written_from_the_source_while_lineage_is_down_holds_the_erasure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: Callable[[httpx.Client, str], None]
) -> None:
    """Every announcement is published on a bus no lineage consumes, so lineage records no edge: the verification names
    cleaned from what the catalog recorded when cleaned's write declared raw, and the obligation stays open."""
    joined_bus(monkeypatch)
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        for namespace in ("lance_catalog$acme-raw", "lance_catalog$acme-cleaned"):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$acme-raw$events/create", content=_rows(), headers=_ARROW).status_code == 200
        write(catalog, catalog_url)
        cleaned = catalog.post("/v1/table/lance_catalog$acme-cleaned$pages/describe", json={}).json()["properties"][UUID_PROPERTY]
        source = _obligation(catalog, "lance_catalog$acme-raw$events")

        verified = catalog.post(f"/management/v1/table/lance_catalog$acme-raw$events/erasure/{source}/verify", json={"predicate": _GONE})

    assert verified.status_code == 200, verified.text
    report, obligation = verified.json()["report"], verified.json()["obligation"]
    assert [(surface["surface"], surface["outcome"]) for surface in report["surfaces"] if surface["surface"].startswith("derived")] == [
        (f"derived:{cleaned}", "failed")
    ]
    assert ([surface["surface"] for surface in obligation["open_surfaces"]], obligation["state"]) == ([f"derived:{cleaned}"], "open")
    assert "A write that declared no inputs derives from nothing" in report["derived_scope"]


class _Counter:
    """The paging counter's instrument, recording what it is handed."""

    def __init__(self) -> None:
        self.added: list[int] = []

    def add(self, amount: int, attributes: Mapping[str, str] | None = None, context: object = None) -> None:
        self.added.append(amount)


def test_a_table_lineage_records_after_the_release_from_a_run_before_it_is_listed_on_the_obligation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An outside producer's run wrote ``acme-cleaned$late`` from raw before the release and lineage records it after: the
    reconciler's tick lists it on the released obligation once, and pages."""
    from catalog.api import commit_reconcile

    counted = _Counter()
    monkeypatch.setattr(commit_reconcile, "_late_derived", counted)
    joined_bus(monkeypatch)
    with (
        live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
        live_lineage(catalog_url, monkeypatch) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        assert catalog.post("/v1/namespace/lance_catalog$acme-raw/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$acme-raw$events/create", content=_rows(), headers=_ARROW).status_code == 200
        uuid_of = {
            "lance_catalog$acme-raw$events": catalog.post("/v1/table/lance_catalog$acme-raw$events/describe", json={}).json()["properties"][UUID_PROPERTY]
        }
        uuid_of |= declared(catalog_url, "lance_catalog$acme-cleaned$late")
        source = _obligation(catalog, "lance_catalog$acme-raw$events")
        released = catalog.post(f"/management/v1/table/lance_catalog$acme-raw$events/erasure/{source}/verify", json={"predicate": _GONE})
        assert released.json()["obligation"]["state"] == "released", released.text
        run_at = datetime.fromisoformat(released.json()["obligation"]["released_at"]) - timedelta(minutes=1)
        outside = run_event(
            str(uuid.uuid4()), "COMPLETE", run_at.isoformat(), outputs=["lance_catalog$acme-cleaned$late"], inputs=("lance_catalog$acme-raw$events",)
        )
        assert lineage.post("/api/v1/lineage", json=_stamped(outside, uuid_of)).status_code == 201

        ticks = [catalog_tick(catalog_url, commit_reconcile.tick).late_derived for _ in range(2)]
        listed = catalog.get("/management/v1/erasures").json()["obligations"]

    # The catalog creates the series at 0 when it starts, so the find is an increase the alert reads.
    assert (ticks, counted.added) == ([1, 0], [0, 1])
    assert [(o["obligation_id"], o["late_derived"]) for o in listed] == [(source, [uuid_of["lance_catalog$acme-cleaned$late"]])]


def test_an_answer_that_hides_a_derived_table_says_it_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """The eraser reads the source and the table it fed, never the table that one fed in turn, on a real OpenFGA store of
    the test's own."""
    from lineage.models import parse_event

    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url:
        uuid_of = declared(catalog_url, "lance_catalog$acme-raw$events", "lance_catalog$acme-cleaned$pages", "lance_catalog$acme-served$catalog")
        granted = {("dex~eraser", "reader", f"table:{uuid_of[name]}") for name in ("lance_catalog$acme-raw$events", "lance_catalog$acme-cleaned$pages")}
        chain = [
            run_event(
                str(uuid.uuid4()),
                "COMPLETE",
                "2026-10-15T10:00:01+00:00",
                outputs=["lance_catalog$acme-cleaned$pages"],
                inputs=("lance_catalog$acme-raw$events",),
            ),
            run_event(
                str(uuid.uuid4()),
                "COMPLETE",
                "2026-10-15T10:00:02+00:00",
                outputs=["lance_catalog$acme-served$catalog"],
                inputs=("lance_catalog$acme-cleaned$pages",),
            ),
        ]
        with governed_lineage(catalog_url, monkeypatch, granted, openfga_store) as lineage:
            for event in chain:
                asyncio.run(app_of(lineage).state.provenance.record(parse_event(_stamped(event, uuid_of)), aliased=()))

            answer = lineage.get("/tables/lance_catalog$acme-raw$events/derived", headers={SUBJECT_HEADER: "dex~eraser"})

    assert answer.status_code == 200, answer.text
    assert answer.json() == {
        "table": "lance_catalog$acme-raw$events",
        "uuid": uuid_of["lance_catalog$acme-raw$events"],
        "derived": [{"table": "lance_catalog$acme-cleaned$pages", "uuid": uuid_of["lance_catalog$acme-cleaned$pages"], "depth": 1}],
        "complete": False,
    }


def test_a_renamed_source_answers_what_it_fed_under_both_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read through lineage's FGA gate by a caller granted on the tables' uuids, which a rename keeps and so moves no
    grant: the write recorded under the old name is answered as the table's."""
    joined_bus(monkeypatch)
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        with live_lineage(catalog_url, monkeypatch):
            for namespace in ("lance_catalog$acme-raw", "lance_catalog$acme-cleaned"):
                assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
            assert catalog.post("/v1/table/lance_catalog$acme-raw$t/create", content=_rows(), headers=_ARROW).status_code == 200
            assert (
                catalog.post("/v1/table/lance_catalog$acme-cleaned$x/create", content=_rows(), headers=_run("x-1", "lance_catalog$acme-raw$t")).status_code
                == 200
            )
            assert catalog.post("/v1/table/lance_catalog$acme-raw$t/rename", json={"new_table_name": "u"}).status_code == 200
            assert (
                catalog.post("/v1/table/lance_catalog$acme-cleaned$y/create", content=_rows(), headers=_run("y-1", "lance_catalog$acme-raw$u")).status_code
                == 200
            )
        uuid_of = {
            name: catalog.post(f"/v1/table/{name}/describe", json={}).json()["properties"][UUID_PROPERTY]
            for name in ("lance_catalog$acme-raw$u", "lance_catalog$acme-cleaned$x", "lance_catalog$acme-cleaned$y")
        }
        granted = {("dex~owner", "can_get_metadata", f"table:{table_uuid}") for table_uuid in uuid_of.values()}
        with governed_lineage(catalog_url, monkeypatch, granted) as lineage:
            derived = lineage.get("/tables/lance_catalog$acme-raw$u/derived", headers={SUBJECT_HEADER: "dex~owner"})
            scoped = lineage.get("/events", params={"table": "lance_catalog$acme-raw$u", "since": 0}, headers={SUBJECT_HEADER: "dex~owner"})

    assert derived.status_code == 200, derived.text
    assert derived.json() == {
        "table": "lance_catalog$acme-raw$u",
        "uuid": uuid_of["lance_catalog$acme-raw$u"],
        "derived": [
            {"table": "lance_catalog$acme-cleaned$x", "uuid": uuid_of["lance_catalog$acme-cleaned$x"], "depth": 1},
            {"table": "lance_catalog$acme-cleaned$y", "uuid": uuid_of["lance_catalog$acme-cleaned$y"], "depth": 1},
        ],
        "complete": True,
    }
    assert scoped.status_code == 200, scoped.text
    cleaned = [row for row in scoped.json()["events"] if row["outputs"][0].startswith("lance_catalog$acme-cleaned$")]
    writes = {(tuple(row["inputs"]), tuple(row["outputs"])) for row in cleaned}
    assert writes == {
        (("lance_catalog$acme-raw$t",), ("lance_catalog$acme-cleaned$x",)),
        (("lance_catalog$acme-raw$u",), ("lance_catalog$acme-cleaned$y",)),
    }, scoped.json()["events"]
