"""A door that hands its request to the upstream implementation decides the request's ``branch``.

The upstream ``dir`` implementation disregards ``branch`` on many ops (measured 2026-08-31: query, the
plan ops, stats and the index builds), so a door that passes the whole spec request to ``native.call``
answers for MAIN with a 200: a branch-scoped write lands on main and a branch-scoped read returns
main's numbers. Under D3 a branch
is a parameter of the table's verbs (``lance_docs/ns_catalog/spec.yaml:2323-2333``), so each door either
serves the ref or refuses it with ``refuse_a_branch_this_door_cannot_honour`` (spec code 0).

The probe is a branch the table does not have. A door that ignores ``branch`` answers it from main with
a 200; a refusing door answers 0 ``Unsupported``; a serving door opens the ref and finds nothing there.
Every door below delegates to ``native.call`` with a branch-carrying spec model, which is the shape the
defect takes; a door that opens its own ref goes through ``open_dataset`` and answers code 22 from there
(``tests/integration/test_column_branch.py``).
"""

from __future__ import annotations

import io
from typing import Any

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


_T = "/v1/table/g1$t"
_VECTOR = {"single_vector": [0.1, 0.2]}
_UNSUPPORTED = 0
_TABLE_NOT_FOUND = 4
_BRANCH_NOT_FOUND = 22


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture
def table(real_ns_client: TestClient) -> tuple[TestClient, str]:
    """A real table at ``g1$t`` with a vector column and a named scalar index, and its location."""
    client = real_ns_client
    assert client.post("/v1/namespace/g1/create", json={}).status_code == 200
    rows = pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "v": ["a", "b", "c"],
            "vec": pa.array([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]], pa.list_(pa.float32(), 2)),
        }
    )
    created = client.post(f"{_T}/create?mode=overwrite", content=_ipc(rows), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert created.status_code == 200, created.text
    indexed = client.post(f"{_T}/create_scalar_index", json={"column": "id", "index_type": "BTREE", "name": "id_idx"})
    assert indexed.status_code == 200, indexed.text
    described = client.post(f"{_T}/describe", json={})
    assert described.status_code == 200, described.text
    return client, described.json()["table_uri"]


@pytest.mark.parametrize(
    ("path", "request_kwargs", "status", "code"),
    [
        pytest.param("/query", {"json": {"branch": "ghost", "k": 1, "vector": _VECTOR}}, 406, _UNSUPPORTED, id="query"),
        pytest.param("/explain_plan", {"json": {"branch": "ghost", "query": {"k": 1, "vector": _VECTOR}}}, 406, _UNSUPPORTED, id="explain_plan"),
        pytest.param("/analyze_plan", {"json": {"branch": "ghost", "k": 1, "vector": _VECTOR}}, 406, _UNSUPPORTED, id="analyze_plan"),
        pytest.param("/backfill_column", {"json": {"branch": "ghost", "column": "v"}}, 406, _UNSUPPORTED, id="backfill_column"),
        pytest.param("/create_index", {"json": {"branch": "ghost", "column": "vec", "index_type": "IVF_FLAT"}}, 406, _UNSUPPORTED, id="create_index"),
        pytest.param("/create_scalar_index", {"json": {"branch": "ghost", "column": "v", "index_type": "BTREE"}}, 406, _UNSUPPORTED, id="create_scalar_index"),
        pytest.param("/index/list", {"json": {"branch": "ghost"}}, 406, _UNSUPPORTED, id="index_list"),
        pytest.param("/index/id_idx/stats", {"json": {"branch": "ghost"}}, 406, _UNSUPPORTED, id="index_stats"),
        pytest.param("/index/id_idx/drop", {"json": {"branch": "ghost"}}, 406, _UNSUPPORTED, id="index_drop"),
        pytest.param("/stats", {"json": {"branch": "ghost"}}, 406, _UNSUPPORTED, id="stats"),
        pytest.param("/schema_metadata/update", {"json": {"branch": "ghost", "metadata": {"k": "v"}}}, 404, _BRANCH_NOT_FOUND, id="schema_metadata"),
        pytest.param("/restore", {"json": {"branch": "ghost", "version": 1}}, 404, _BRANCH_NOT_FOUND, id="restore"),
        # Served by the upstream op, which reports a missing branch as TableNotFound rather than code 22.
        pytest.param("/version/list?branch=ghost", {"json": {}}, 404, _TABLE_NOT_FOUND, id="version_list"),
        pytest.param("/version/describe", {"json": {"branch": "ghost", "version": 1}}, 404, _TABLE_NOT_FOUND, id="version_describe"),
    ],
)
def test_a_branch_the_table_lacks_is_never_answered_from_main(
    table: tuple[TestClient, str], path: str, request_kwargs: dict[str, Any], status: int, code: int
) -> None:
    client, location = table
    main_version = lance.dataset(location).version

    r = client.post(f"{_T}{path}", **request_kwargs)

    assert (r.status_code, r.json().get("code")) == (status, code), r.text
    assert lance.dataset(location).version == main_version, "main took a commit the request scoped to a branch"
