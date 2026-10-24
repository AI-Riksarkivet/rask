"""[[LH-286]] A table's data base is part of its storage: every door reaches it under the catalog's own identity, as the root.

Lance opens every base a manifest declares under the dataset's own options unless a base is given options of its own
(``lance_docs/guide.md`` § Per-Base Configuration: "every base inherits the unscoped options"), and a data base on the
estate's store is its tables' storage, so the catalog holds no credential for one (owner, 2026-10-24). The pylance doors,
the native doors and the vend each serve a table whose fragments sit on a data base as they serve any other: rows, a
count and a direct credential, never a refusal and never a second identity.

Driven on the two-store estate (``conftest.two_store_catalog``), the catalog's session under a role allowed both buckets
as the deployed catalog's canned policy allows its whole store: the table's root on ``estate``, its fragments on
``second/data``. Rows are compared, not counted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import IssuedSession


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
REPLACED = pa.table({"id": pa.array([7, 8], pa.int64()), "v": ["x", "y"]})


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _rows(content: bytes) -> list[tuple[int, str]]:
    read = ipc.open_file(pa.BufferReader(content)).read_all()
    return sorted(zip(read.column("id").to_pylist(), read.column("v").to_pylist(), strict=True))


@pytest.mark.parametrize(
    ("door", "body", "expected"),
    [
        pytest.param(
            "/management/v1/table/{table}/changes",
            {"begin_version": 0, "end_version": 2, "kind": "inserted", "columns": ["id", "v"]},
            [(7, "x"), (8, "y")],
            id="a-pylance-door-reads-the-rows-an-overwrite-wrote-to-the-base",
        ),
        pytest.param("/v1/table/{table}/count_rows", {"predicate": "id > 7"}, "1", id="the-native-count-reads-the-base"),
        pytest.param(
            "/v1/table/{table}/query",
            {"k": 10, "vector": {}, "columns": {"column_names": ["id", "v"]}},
            [(7, "x"), (8, "y")],
            id="the-native-query-reads-the-base",
        ),
        pytest.param("/management/v1/table/{table}/credentials", None, "direct", id="the-vend-is-direct"),
    ],
)
def test_every_door_serves_a_table_whose_fragments_sit_on_a_data_base(
    two_store_catalog: TestClient, second_base: str, door: str, body: dict[str, object] | None, expected: object
) -> None:
    table = f"mb${uuid.uuid4().hex[:12]}"
    created = two_store_catalog.post(f"/v1/table/{table}/create?data_base={second_base}", content=_ipc(ROWS), headers=ARROW)
    assert created.status_code == 200, created.text
    replaced = two_store_catalog.post(f"/v1/table/{table}/create?mode=overwrite&data_base={second_base}", content=_ipc(REPLACED), headers=ARROW)
    assert replaced.status_code == 200, replaced.text
    # The one call that would reach the store: a vend that minted answers `direct` with these.
    two_store_catalog.app.state.vendor._exchange = lambda *_a, **_kw: IssuedSession(  # noqa: SLF001
        key_pair=S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK"), session_token=SecretStr("ST")), expiration=datetime(2030, 1, 1, tzinfo=UTC)
    )

    response = two_store_catalog.post(door.format(table=table), json=body)

    assert response.status_code == 200, response.text
    if isinstance(expected, list):
        assert _rows(response.content) == expected
    elif door.endswith("/credentials"):
        assert response.json()["mode"] == expected, response.text
    else:
        assert response.text == expected
