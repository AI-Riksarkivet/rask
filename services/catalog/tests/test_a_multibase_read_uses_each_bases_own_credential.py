"""[[LH-273]] A multi-base table's data base is read and written only under that base's own credential.

The write side composes ``base_store_params`` from the configured credential references; a read that
opens the same table with the estate's options alone sends the estate key to the second store, which
refuses it — or, where the estate key does reach the base, reads it under an identity nobody configured
for it. So every pylance open composes the map from the manifest's base paths, an overwrite writes the
base under its own key as a create does, and the doors that cannot carry the map refuse such a table
rather than reach the base under the estate key: the native doors answer 406, and the vend, whose STS
credential is the estate role's, answers ``server_mediated``.

Driven on two stores that each answer only their own key (``conftest.two_stores``): the table's root on
``estate``, its fragments on ``second/data``. Rows are compared, not counted.
"""

from __future__ import annotations

import uuid

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
REPLACED = pa.table({"id": pa.array([7, 8], pa.int64()), "v": ["x", "y"]})


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.mark.parametrize(
    ("overwrite", "door", "body", "expected"),
    [
        pytest.param(
            False,
            "/management/v1/table/{table}/changes",
            {"begin_version": 0, "end_version": 1, "kind": "inserted", "columns": ["id", "v"]},
            [(1, "a"), (2, "b"), (3, "c")],
            id="pylance-door-reads-the-rows",
        ),
        pytest.param(
            True,
            "/management/v1/table/{table}/changes",
            {"begin_version": 0, "end_version": 2, "kind": "inserted", "columns": ["id", "v"]},
            [(7, "x"), (8, "y")],
            id="an-overwrite-writes-the-base-under-its-own-credential",
        ),
        pytest.param(False, "/v1/table/{table}/count_rows", {}, 406, id="native-door-refuses-rather-than-read-under-the-estate-key"),
        pytest.param(False, "/management/v1/table/{table}/credentials", None, "server_mediated", id="the-vend-answers-server-mediated"),
    ],
)
def test_a_read_of_a_table_on_a_second_store_uses_that_stores_credential(
    two_store_catalog: TestClient, second_base: str, overwrite: bool, door: str, body: dict[str, object] | None, expected: object
) -> None:
    table = f"mb${uuid.uuid4().hex[:12]}"
    created = two_store_catalog.post(f"/v1/table/{table}/create?data_base={second_base}", content=_ipc(ROWS), headers=ARROW)
    assert created.status_code == 200, created.text
    if overwrite:
        replaced = two_store_catalog.post(f"/v1/table/{table}/create?mode=overwrite&data_base={second_base}", content=_ipc(REPLACED), headers=ARROW)
        assert replaced.status_code == 200, f"the overwrite did not land on the base under its own credential: {replaced.text}"
    # The one call that would reach the store: a vend that minted would answer `direct` with these.
    two_store_catalog.app.state.vendor._assume_role = lambda **_kw: {  # noqa: SLF001
        "Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST", "Expiration": None}
    }

    response = two_store_catalog.post(door.format(table=table), json=body)

    if isinstance(expected, list):
        assert response.status_code == 200, response.text
        read = ipc.open_file(pa.BufferReader(response.content)).read_all()
        assert sorted(zip(read.column("id").to_pylist(), read.column("v").to_pylist(), strict=True)) == expected
    elif expected == 406:
        assert response.status_code == 406, response.text
        assert "own credential" in response.json()["detail"], response.text
    else:
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == expected, response.text
