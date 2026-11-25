"""An Overwrite of an existing table is a new version of the same table ([[LH-242]]).

Two doors take an Overwrite mode, and both replace every row at the tip: `create?mode=Overwrite`
(spec.yaml `CreateTableRequest.mode`) and `insert?mode=overwrite` (`InsertIntoTableRequest.mode`).
Measured before the fix: the lineage event called the first a `create_table` and the second an `insert`, neither
carrying the standard `OVERWRITE` lifecycle state.

The table's earlier versions stay readable by time travel either way, so the catalog serves the
Overwrite as a new version of the same table: its record says the table was overwritten, not created, and it keeps
the uuid the catalog minted for it ([[LH-519]]). Protection guards the table's drop, not its rows (ADR 0221): a
protected table takes an overwrite like any other write its grants admit.
No table came into being, so neither door announces one on the control lane: the lineage `OVERWRITE` event is
the overwrite's announcement, as Lance records it as one more version of the same dataset ([[LH-415]], ADR 0099).

Driven through the real catalog app on a real `dir` namespace, with the lineage event captured on the
wire: the real `BusEmitter` builds it and publishes it on the bus double.
"""

import io

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from catalog.core.lineage_emit import BusEmitter
from tests.app_state import app_of
from tests.bus_double import BusDouble
from tests.control_emitter_double import ControlEmitterDouble


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lance_catalog$lh242$t"
#: The two doors, by the query that puts each in Overwrite mode.
DOORS = {"create": "create?mode=overwrite", "insert": "insert?mode=overwrite"}


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": [f"v{i}" for i in ids]})


@pytest.fixture
def bus(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> BusDouble:
    """The bus the catalog publishes on, which keeps every lineage event as the lineage service would receive it."""
    bus = BusDouble()
    emitter = BusEmitter(
        bus,
        "lineage.events.v1",
        job_namespace="lance-catalog",
        dataset_namespace="lance-namespace://localhost/api/catalog",
        timeout_seconds=5.0,
    )
    monkeypatch.setattr(app_of(real_ns_client).state, "lineage_emitter", emitter, raising=False)
    return bus


@pytest.fixture
def protected(real_ns_client: TestClient) -> str:
    """`lh242$t` at version 1 with three rows, protected through the catalog's own door. Returns its location."""
    assert real_ns_client.post("/v1/namespace/lance_catalog$lh242/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create?mode=create", content=_ipc(_rows(1, 2, 3)), headers=ARROW)
    assert created.status_code == 200, created.text
    armed = real_ns_client.post(f"/management/v1/table/{TABLE}/protection", json={"protected": True})
    assert armed.status_code == 200, armed.text
    return str(created.json()["location"])


@pytest.mark.parametrize("door", list(DOORS))
def test_protection_guards_a_tables_drop_and_not_an_overwrite_of_its_rows(real_ns_client: TestClient, protected: str, door: str) -> None:
    """Protection keeps the table, and an overwrite keeps it: one more version, every earlier one still read. What may
    write its rows is its grants' and its data contract's to say."""
    answer = real_ns_client.post(f"/v1/table/{TABLE}/{DOORS[door]}", content=_ipc(_rows(9)), headers=ARROW)
    dropped = real_ns_client.post(f"/v1/table/{TABLE}/drop", json={})

    assert answer.status_code == 200, f"{door}: {answer.text}"
    assert (dropped.status_code, dropped.json()["code"]) == (409, 19), dropped.text  # InvalidTableState
    after = lance.dataset(protected)
    assert sorted(after.to_table()["id"].to_pylist()) == [9]
    assert sorted(lance.dataset(protected, version=1).to_table()["id"].to_pylist()) == [1, 2, 3]


def _uuid(client: TestClient) -> str:
    described = client.post(f"/v1/table/{TABLE}/describe", json={})
    assert described.status_code == 200, described.text
    properties = described.json().get("properties") or {}
    assert "rask.uuid" in properties, f"{TABLE} answers no uuid: {properties}"
    return str(properties["rask.uuid"])


@pytest.mark.parametrize("door", list(DOORS))
def test_an_overwrite_is_recorded_as_an_overwrite_of_the_same_table(
    real_ns_client: TestClient, protected: str, bus: BusDouble, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    minted = _uuid(real_ns_client)
    control = ControlEmitterDouble()
    monkeypatch.setattr(app_of(real_ns_client).state, "control_emitter", control, raising=False)

    answer = real_ns_client.post(f"/v1/table/{TABLE}/{DOORS[door]}", content=_ipc(_rows(9)), headers=ARROW)

    assert answer.status_code == 200, f"{door}: {answer.text}"
    assert _uuid(real_ns_client) == minted, f"{door}: the overwritten table answers another uuid"
    version = int(answer.json()["version"])
    assert sorted(lance.dataset(protected, version=1).to_table()["id"].to_pylist()) == [1, 2, 3], "the table's history did not survive"
    assert len(bus.events) == 1, f"{door}: expected one lineage event, got {[e.get('job') or e.get('dataset') for e in bus.events]}"
    event = bus.events[0]
    assert "run" in event, f"{door}: recorded as a static DatasetEvent, which carries no WROTE edge: {event}"
    assert event["run"]["facets"]["rask_lance"]["operation"] == "overwrite_table", event["run"]["facets"]["rask_lance"]
    facets = event["outputs"][0]["facets"]
    assert event["outputs"][0]["name"] == TABLE
    assert facets.get("lifecycleStateChange", {}).get("lifecycleStateChange") == "OVERWRITE", facets.get("lifecycleStateChange")
    assert facets["version"]["datasetVersion"] == str(version), "the WROTE edge does not name the version the overwrite made"
    assert [e.action for e in control.events] == [], (
        f"{door}: an overwrite of an existing table was announced on the control lane: {[e.action for e in control.events]}"
    )


def test_an_overwrite_create_of_an_absent_table_is_announced_as_a_created_table(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert real_ns_client.post("/v1/namespace/lance_catalog$lh242/create", json={}).status_code == 200
    control = ControlEmitterDouble()
    monkeypatch.setattr(app_of(real_ns_client).state, "control_emitter", control, raising=False)

    answer = real_ns_client.post(f"/v1/table/{TABLE}/{DOORS['create']}", content=_ipc(_rows(1)), headers=ARROW)

    assert answer.status_code == 200, answer.text
    assert [e.action for e in control.events] == ["table_created"], [e.action for e in control.events]
