"""Concurrent renames of one table leave one live id on its location ([[LH-204]]).

The `dir` backend arbitrates only ADDs of an object id: measured on pylance 12.0.0 (lh204 m1), eight
barrier-threaded renames of one source, each retiring the source before registering its destination,
all answered 200 and left eight live ids on one dataset in 4 of 4 rounds. A destructive drop of any of
them then deletes the bytes the other seven resolve to. Renames into ONE destination name race the same
way: each must tell its own claim from another request's, and a loser must not put the source back over
the winner's finished rename.

Driven through the real catalog app on a real `dir` namespace, with every rename released by one
barrier, and judged by what the catalog then resolves.
"""

from __future__ import annotations

import io
import threading

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance_namespace import ConcurrentModificationError


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
RENAMES = 8


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.parametrize("same_destination", [pytest.param(False, id="distinct-destinations"), pytest.param(True, id="one-destination")])
def test_concurrent_renames_of_one_table_leave_one_live_id_on_its_location(real_ns_client: TestClient, same_destination: bool) -> None:
    client = real_ns_client
    assert client.post("/v1/namespace/ns1/create", json={}).status_code == 200
    created = client.post("/v1/table/ns1$src/create?mode=create", content=_ipc(pa.table({"id": [1, 2, 3]})), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    barrier = threading.Barrier(RENAMES)
    answers: dict[int, tuple[int, dict[str, object]]] = {}

    def _rename(i: int) -> None:
        barrier.wait()
        response = client.post("/v1/table/ns1$src/rename", json={"new_table_name": "d0" if same_destination else f"d{i}"})
        answers[i] = (response.status_code, response.json())

    threads = [threading.Thread(target=_rename, args=(i,)) for i in range(RENAMES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    resolving = {}
    for name in ["src", *(f"d{i}" for i in range(RENAMES))]:
        described = client.post(f"/v1/table/ns1${name}/describe", json={})
        if described.status_code == 200:
            resolving[name] = described.json()["location"]
    statuses = sorted(status for status, _ in answers.values())
    losers = [body for status, body in answers.values() if status == 409]
    assert len(resolving) == 1, f"{len(resolving)} live ids after {RENAMES} concurrent renames: {resolving}"
    assert list(resolving.values()) == [location]
    # A loser that reaches the door after the winner has finished finds no source: 404, also a refusal.
    assert len(statuses) == RENAMES and statuses.count(200) == 1 and set(statuses) <= {200, 404, 409}, answers
    assert {body.get("code") for body in losers} <= {ConcurrentModificationError.code}, losers
