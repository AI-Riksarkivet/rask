"""A mode outside its door's vocabulary is refused as InvalidInput, and the door does nothing.

Every `mode` and `behavior` the spec defines on a door the catalog serves is a closed set. Each carries
"Case insensitive, supports both PascalCase and snake_case. Valid values are: …" in
`lance_docs/ns_catalog/spec.yaml` — CreateTableRequest and CreateNamespaceRequest (Create, ExistOk,
Overwrite), RegisterTableRequest (Create, Overwrite), DropNamespaceRequest (mode: Fail, Skip; behavior:
Restrict, Cascade) and InsertIntoTable (Append, Overwrite). A value outside the set is a malformed
request, and the spec's code for that is 13 InvalidInput, which the problem handlers answer 400. Owner
ruling 2026-09-25: that is the answer on every one of these doors.

A TYPO FOLDED TO THE DEFAULT ANSWERS A DIFFERENT REQUEST. On these doors the difference is a write the
caller did not ask for, reported as success: `?mode=Overwrit` creates a table, `{"mode": "PURGE"}`
drops a namespace, `{"behavior": "Cascde"}` drops an empty one, and `mode: "ExistOk"` — a create word
the spec does not give `register` — attaches a location.

THE UPSTREAM BACKEND ALREADY ANSWERS THIS WAY where it reads the field. Measured on pylance 12.0.0's
`DirectoryNamespace`: `create_table(mode="bogus")` raises `InvalidInputError: Unsupported create_table
mode 'bogus'. Supported modes are: 'Create', 'ExistOk', 'Overwrite'`, and `insert_into_table` refuses
`create` and `bogus` with the same class. It ignores `mode` and `behavior` on `create_namespace` and
`drop_namespace`, so on those doors the catalog's parser is the only one there is. The branch arm of
`insert` runs pylance's `LanceDataset.insert` in-process, which raises a bare `ValueError` for an
unknown mode, so the same typo answered 500 there and 400 on main.

Driven over HTTP against a real `dir` backend, so the status, the spec `code` and the absence of the
side effect are each read where a client would read them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance_namespace import InsertIntoTableRequest, InvalidInputError, connect

from catalog.services.dataplane import create_table, insert_into_table, open_dataset


if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient
    from httpx import Response

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
INVALID_INPUT = 13


def _rows(n: int = 3) -> bytes:
    table = pa.table({"id": pa.array(range(n), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _refused(response: Response, value: str) -> dict[str, Any]:
    """400 with the spec's InvalidInput code, and a detail that names the value it refused."""
    assert response.status_code == 400, f"expected a 400 refusal, got {response.status_code}: {response.text[:400]}"
    body: dict[str, Any] = response.json()
    assert body.get("code") == INVALID_INPUT, f"a malformed mode is InvalidInput (13): {body}"
    assert value in str(body.get("detail")), f"the refusal must name the value it refused: {body.get('detail')!r}"
    return body


def _namespace(client: TestClient, ns_id: str) -> None:
    created = client.post(f"/v1/namespace/{ns_id}/create", json={})
    assert created.status_code == 200, created.text


def _table(client: TestClient, table_id: str) -> None:
    created = client.post(f"/v1/table/{table_id}/create", content=_rows(), headers=ARROW_STREAM)
    assert created.status_code == 200, created.text


def _exists(client: TestClient, kind: str, object_id: str) -> bool:
    status = client.post(f"/v1/{kind}/{object_id}/exists", json={}).status_code
    assert status in (200, 404), f"exists answered {status}, which says nothing about existence"
    return status == 200


def _rows_on(client: TestClient, table_id: str, branch: str | None = None) -> int:
    counted = client.post(f"/v1/table/{table_id}/count_rows", json={"branch": branch} if branch else {})
    assert counted.status_code == 200, counted.text
    return int(counted.text)


@pytest.mark.parametrize("mode", ["Overwrit", "exists_ok", "Replace"])
def test_a_create_mode_outside_the_vocabulary_creates_no_table(real_ns_client: TestClient, mode: str) -> None:
    """On a free id the fold to `Create` was a 200 and a table nobody asked for."""
    _namespace(real_ns_client, "db")

    _refused(real_ns_client.post(f"/v1/table/db$t/create?mode={mode}", content=_rows(), headers=ARROW_STREAM), mode)

    assert not _exists(real_ns_client, "table", "db$t"), "a refused create must leave no table behind"


def test_a_create_mode_outside_the_vocabulary_is_refused_on_a_taken_id_too(real_ns_client: TestClient) -> None:
    """The refusal is about the request's shape, so it cannot depend on whether the id is taken. On a
    taken id the fold answered 409, which tells the caller the name collides when the mode was the fault."""
    _namespace(real_ns_client, "db")
    _table(real_ns_client, "db$t")

    _refused(real_ns_client.post("/v1/table/db$t/create?mode=Overwrit", content=_rows(7), headers=ARROW_STREAM), "Overwrit")

    assert _rows_on(real_ns_client, "db$t") == 3, "a refused create must leave the existing table's data alone"


def test_a_namespace_create_mode_outside_the_vocabulary_creates_no_namespace(real_ns_client: TestClient) -> None:
    _refused(real_ns_client.post("/v1/namespace/db/create", json={"mode": "ExistsOk"}), "ExistsOk")

    assert not _exists(real_ns_client, "namespace", "db"), "a refused create must leave no namespace behind"


@pytest.mark.parametrize(
    ("body", "value"),
    [
        pytest.param({"mode": "PURGE"}, "PURGE", id="mode"),
        pytest.param({"behavior": "Cascde"}, "Cascde", id="behavior"),
    ],
)
def test_a_drop_field_outside_its_vocabulary_drops_nothing(real_ns_client: TestClient, body: dict[str, str], value: str) -> None:
    """Both of the drop's fields are closed. `PURGE` is the live case: `purge` is a QUERY parameter, and a
    caller who spelled it as the body's `mode` got a recoverable drop reported as success."""
    _namespace(real_ns_client, "db")

    _refused(real_ns_client.post("/v1/namespace/db/drop", json=body), value)

    assert _exists(real_ns_client, "namespace", "db"), "a refused drop must leave the namespace in place"


@pytest.mark.parametrize("mode", ["ExistOk", "exist_ok", "Overwrit"])
def test_a_register_mode_outside_its_two_attaches_nothing(real_ns_client: TestClient, tmp_path: Path, mode: str) -> None:
    """`register` has two modes, not `create`'s three. `ExistOk` is a real word on the create doors, so a
    caller reusing it here is the likeliest way to reach this refusal."""
    _namespace(real_ns_client, "db")
    lance.write_dataset(pa.table({"id": pa.array([1, 2], pa.int64())}), str(tmp_path / "ext"), data_storage_version="2.2", enable_stable_row_ids=True)

    _refused(real_ns_client.post("/v1/table/db$r/register", json={"location": "ext", "mode": mode}), mode)

    assert not _exists(real_ns_client, "table", "db$r"), "a refused register must attach nothing"


@pytest.mark.parametrize("branch", [None, "work"], ids=["main", "branch"])
@pytest.mark.parametrize("mode", ["exist_ok", "create", "Appnd"])
def test_an_insert_mode_outside_the_vocabulary_is_refused_on_both_arms(real_ns_client: TestClient, mode: str, branch: str | None) -> None:
    """The two arms of `insert` reach two different parsers, so the door parses the mode before either.
    `create` is worth its own case: it is in pylance's write-mode vocabulary but not in the spec's."""
    _namespace(real_ns_client, "db")
    _table(real_ns_client, "db$t")
    if branch is not None:
        made = real_ns_client.post("/v1/table/db$t/branches/create", json={"name": branch})
        assert made.status_code == 200, made.text
    query = f"&branch={branch}" if branch else ""

    _refused(real_ns_client.post(f"/v1/table/db$t/insert?mode={mode}{query}", content=_rows(5), headers=ARROW_STREAM), mode)

    assert _rows_on(real_ns_client, "db$t", branch) == 3, "a refused insert must write no rows"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param("/v1/table/ghost$t/create?mode=Overwrit", "Overwrit", id="create-under-a-missing-namespace"),
        pytest.param("/v1/table/db$ghost/insert?mode=Appnd", "Appnd", id="insert-into-a-missing-table"),
    ],
)
def test_the_refusal_is_a_shape_answer_ahead_of_every_lookup(real_ns_client: TestClient, path: str, value: str) -> None:
    """Parsed before the door reads anything, so a malformed mode is reported as malformed whatever the
    id resolves to — not as the 404 a lookup would answer first."""
    _namespace(real_ns_client, "db")

    _refused(real_ns_client.post(path, content=_rows(), headers=ARROW_STREAM), value)


@pytest.mark.parametrize("branch", [None, "work"], ids=["main", "branch"])
def test_the_data_plane_refuses_it_without_the_door(tmp_path: Path, branch: str | None) -> None:
    """`dataplane.insert_into_table` is a seam of its own, so its branch arm must not depend on the door
    having parsed first: handed the raw value, pylance's `insert` answers a bare ValueError, which is a 500."""
    ns = connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, ["t"], _rows(), mode="create")
    if branch is not None:
        open_dataset(ns, {}, ["t"]).create_branch(branch, None)

    with pytest.raises(InvalidInputError):
        insert_into_table(ns, {}, InsertIntoTableRequest(id=["t"], mode="Appnd", branch=branch), _rows(5))

    assert open_dataset(ns, {}, ["t"], branch=branch).count_rows() == 3, "a refused insert must write no rows"
