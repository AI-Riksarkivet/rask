"""[[LH-258]] Each edge path a stock client reaches answers the spec's code, on the real catalog app over real pylance.

A generated Lance client dispatches on `code` (`lance_docs/ns_catalog/spec.yaml` ErrorResponse), so a caller's mistake
answered 500 code 18 tells it the server broke, and a request answered 200 with part of it dropped tells it nothing.
Each case below is one path that did one of those (measured 2026-10-25 against main 1585bf91): a pinned
TableExists answered for the latest version, a namespace listing served `limit=0`, a `virtual_column` alter, a typo in a
query's filter and a list_view column each answered 500, a detailed describe left out `stats`, and a branch Overwrite's
row count is the one a client reads to confirm the replace. A query refusal's detail names no storage location.

The races and the panic are driven where they happen: a competing ExistOk create lands between the door's probe and its
declaration, and Lance panics inside the first write.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from catalog.services import dataplane


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
T = "/v1/table/g$t"
_VECTOR: dict[str, Any] = {}


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows(n: int = 3) -> bytes:
    return _ipc(pa.table({"id": pa.array(range(n), pa.int64())}))


@pytest.fixture
def client(real_ns_client: TestClient) -> TestClient:
    """A table `g$t` of three rows with a branch `b`, and a child namespace `g$child`."""
    assert real_ns_client.post("/v1/namespace/g/create", json={}).status_code == 200
    assert real_ns_client.post("/v1/namespace/g$child/create", json={}).status_code == 200
    assert real_ns_client.post(f"{T}/create", content=_rows(), headers=ARROW).status_code == 200
    assert real_ns_client.post(f"{T}/branches/create", json={"name": "b"}).status_code == 200
    return real_ns_client


_LIST_VIEW = _ipc(pa.table({"x": pa.array([[1], [2]], pa.list_view(pa.int64()))}))
_VIRTUAL = {"udf": "x", "udf_name": "f", "udf_version": "1", "input_columns": ["id"], "output_type": "int"}


@pytest.mark.parametrize(
    ("send", "status", "code"),
    [
        pytest.param(lambda c: c.post(f"{T}/exists", json={"version": 999}), 404, 11, id="exists-a-version-the-table-lacks"),
        pytest.param(lambda c: c.get("/v1/namespace/$/list?limit=0"), 400, 13, id="namespace-list-limit-zero"),
        pytest.param(lambda c: c.get("/v1/namespace/g/table/list?limit=0"), 400, 13, id="table-list-limit-zero"),
        pytest.param(
            lambda c: c.post(f"{T}/alter_columns", json={"alterations": [{"path": "id", "virtual_column": _VIRTUAL}]}), 406, 0, id="alter-a-virtual-column"
        ),
        pytest.param(lambda c: c.post(f"{T}/query", json={"k": 1, "filter": "nope > 1", "vector": _VECTOR}), 404, 12, id="query-filters-an-absent-column"),
        pytest.param(
            lambda c: c.post(f"{T}/explain_plan", json={"query": {"k": 1, "filter": "id >>> (", "vector": _VECTOR}}),
            400,
            13,
            id="explain-a-filter-that-does-not-parse",
        ),
        pytest.param(
            lambda c: c.post(f"{T}/analyze_plan", json={"k": 1, "filter": "id = 'x' + 1", "vector": _VECTOR}), 400, 13, id="analyze-a-mistyped-literal"
        ),
        pytest.param(lambda c: c.post("/v1/table/g$lv/create", content=_LIST_VIEW, headers=ARROW), 400, 13, id="create-a-type-lance-does-not-store"),
    ],
)
def test_an_edge_path_answers_the_spec_code(client: TestClient, send: Callable[[TestClient], Any], status: int, code: int) -> None:
    response = send(client)

    assert (response.status_code, response.json().get("code")) == (status, code), response.text
    assert "file://" not in response.json().get("detail", ""), "a refusal's detail names the table's storage location"


@pytest.mark.parametrize(
    ("send", "names_a_namespace"),
    [
        pytest.param(lambda c: c.post("/v1/table/g$child/create", content=_rows(), headers=ARROW), True, id="a-namespace-holds-the-id"),
        # The `dir` backend's `namespace_exists` answers present for a table's id too, so a table holder is checked first.
        pytest.param(lambda c: c.post(f"{T}/declare", json={}), False, id="a-table-holds-the-id"),
    ],
)
def test_a_create_at_a_held_id_names_its_holder(client: TestClient, send: Callable[[TestClient], Any], names_a_namespace: bool) -> None:
    response = send(client)

    assert (response.status_code, response.json()["code"]) == (409, 5), response.text
    assert ("a namespace already holds the id" in response.json()["detail"]) is names_a_namespace, response.text


def test_a_detailed_describe_answers_the_tables_stats(client: TestClient) -> None:
    assert client.post(f"{T}/delete", json={"predicate": "id = 0"}).status_code == 200

    described = client.post(f"{T}/describe", json={"load_detailed_metadata": True})

    assert described.json()["stats"] == {"num_deleted_rows": 1, "num_fragments": 1}, described.text


def test_a_branch_overwrite_reports_the_rows_it_wrote(client: TestClient) -> None:
    inserted = client.post(f"{T}/insert?mode=overwrite&branch=b", content=_rows(5), headers=ARROW)

    assert inserted.json()["num_inserted_rows"] == 5, inserted.text


def test_an_existok_create_that_loses_the_race_keeps_the_winners_table(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both creates find no table at the probe; the winner lands between the loser's probe and its declaration."""
    declare = dataplane.declare_table

    def declared_after_the_winner(*args: Any, **kwargs: Any) -> str:  # noqa: ANN401 — passed through to declare_table
        monkeypatch.setattr(dataplane, "declare_table", declare)
        assert client.post("/v1/table/g$race/create?mode=ExistOk", content=_rows(2), headers=ARROW).status_code == 200
        return declare(*args, **kwargs)

    monkeypatch.setattr(dataplane, "declare_table", declared_after_the_winner)

    lost = client.post("/v1/table/g$race/create?mode=ExistOk", content=_rows(7), headers=ARROW)

    assert (lost.status_code, lost.json()["version"]) == (200, 1), lost.text
    assert client.post("/v1/table/g$race/count_rows", json={}).text == "2"


class _Panic(BaseException):
    """What pyo3 raises for a Rust panic: a ``PanicException``, which derives from BaseException, not Exception."""


def test_a_panic_in_the_first_write_answers_500_and_leaves_the_name_free(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    write = dataplane._write_blob

    def panics(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401 — the write's own signature
        raise _Panic("not yet implemented")

    monkeypatch.setattr(dataplane, "_write_blob", panics)
    panicked = TestClient(client.app, raise_server_exceptions=False).post("/v1/table/g$p/create", content=_rows(), headers=ARROW)
    monkeypatch.setattr(dataplane, "_write_blob", write)

    assert (panicked.status_code, panicked.json()["code"]) == (500, 18), panicked.text
    assert client.post("/v1/table/g$p/create", content=_rows(), headers=ARROW).status_code == 200, "the panicked create left its name declared"
