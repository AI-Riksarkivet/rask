"""An Overwrite of an existing table is a new version of the same table ([[LH-242]]).

Two doors take an Overwrite mode, and both replace every row at the tip: `create?mode=Overwrite`
(spec.yaml `CreateTableRequest.mode`) and `insert?mode=overwrite` (`InsertIntoTableRequest.mode`).
Measured before the fix: on a protected table the drop answered 409 while both overwrites answered 200,
and the lineage event called the first a `create_table` and the second an `insert`, neither carrying the
standard `OVERWRITE` lifecycle state.

The table's earlier versions stay readable by time travel either way, so the catalog serves the
Overwrite as a new version of the same table: its protection holds unless `force=true`, and its record
says the table was overwritten, not created.

Driven through the real catalog app on a real `dir` namespace, with the lineage event captured on the
wire: the real `HttpLineageEmitter` builds and posts it to an `httpx.MockTransport`.
"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from catalog.core.lineage_emit import HttpLineageEmitter


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh242$t"
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
def events(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lineage event the catalog posts, as the lineage service would receive it."""
    posted: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _receive(request: httpx.Request) -> httpx.Response:
        with lock:
            posted.append(json.loads(request.content))
        return httpx.Response(200)

    emitter = HttpLineageEmitter(httpx.AsyncClient(transport=httpx.MockTransport(_receive)), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
    monkeypatch.setattr(real_ns_client.app.state, "lineage_emitter", emitter, raising=False)
    return posted


@pytest.fixture
def protected(real_ns_client: TestClient) -> str:
    """`lh242$t` at version 1 with three rows, protected through the catalog's own door. Returns its location."""
    assert real_ns_client.post("/v1/namespace/lh242/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create?mode=create", content=_ipc(_rows(1, 2, 3)), headers=ARROW)
    assert created.status_code == 200, created.text
    armed = real_ns_client.post(f"/management/v1/table/{TABLE}/protection", json={"protected": True})
    assert armed.status_code == 200, armed.text
    return str(created.json()["location"])


@pytest.mark.parametrize("door", list(DOORS))
def test_a_protected_table_refuses_an_overwrite_without_force(real_ns_client: TestClient, protected: str, door: str) -> None:
    before = lance.dataset(protected)

    answer = real_ns_client.post(f"/v1/table/{TABLE}/{DOORS[door]}", content=_ipc(_rows(9)), headers=ARROW)

    assert answer.status_code == 409, f"{door}: a protected table was overwritten: {answer.status_code} {answer.text}"
    problem = answer.json()
    assert problem["code"] == 19, problem  # InvalidTableState, the code the drop door refuses with
    assert "force=true" in problem["detail"], problem
    after = lance.dataset(protected)
    assert after.version == before.version, f"{door}: refused, but the table moved to v{after.version}"
    assert sorted(after.to_table()["id"].to_pylist()) == [1, 2, 3]


@pytest.mark.parametrize("door", list(DOORS))
def test_a_forced_overwrite_is_recorded_as_an_overwrite_of_the_same_table(
    real_ns_client: TestClient, protected: str, events: list[dict[str, Any]], door: str
) -> None:
    answer = real_ns_client.post(f"/v1/table/{TABLE}/{DOORS[door]}&force=true", content=_ipc(_rows(9)), headers=ARROW)

    assert answer.status_code == 200, f"{door}: force did not release the protection lock: {answer.text}"
    version = int(answer.json()["version"])
    assert sorted(lance.dataset(protected, version=1).to_table()["id"].to_pylist()) == [1, 2, 3], "the table's history did not survive"
    assert len(events) == 1, f"{door}: expected one lineage event, got {[e.get('job') or e.get('dataset') for e in events]}"
    event = events[0]
    assert "run" in event, f"{door}: recorded as a static DatasetEvent, which carries no WROTE edge: {event}"
    assert event["run"]["facets"]["lance"]["operation"] == "overwrite_table", event["run"]["facets"]["lance"]
    facets = event["outputs"][0]["facets"]
    assert event["outputs"][0]["name"] == TABLE
    assert facets.get("lifecycleStateChange", {}).get("lifecycleStateChange") == "OVERWRITE", facets.get("lifecycleStateChange")
    assert facets["version"]["datasetVersion"] == str(version), "the WROTE edge does not name the version the overwrite made"
