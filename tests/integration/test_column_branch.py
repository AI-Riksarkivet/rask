"""``branch`` on the column ops targets the BRANCH — it used to be accepted and silently ignored (#100).

The spec puts an optional ``branch`` on all four schema-evolution requests. ``catalog.core.namespace``'s
``open_dataset`` had no branch parameter, so every one of them opened main: a ``POST .../add_columns`` with
``{"branch": "dev"}`` answered **200** with the column on MAIN and ``dev`` untouched. A silent wrong-target
write is the worst shape of defect available here — the caller is told it worked, and the evidence that it
did not is in a ref they were not looking at.

Driven against the real ``dir`` backend, and the assertions read the BYTES back with pylance rather than
trusting the response: the whole failure mode was a response that looked right. ``dev`` is checked for the
new column, ``main`` is checked for its ABSENCE, and the reported version is checked against the branch's
own sequence — the version is the part that silently reverts if only the write is threaded and the read-back
is not.
"""

from __future__ import annotations

import io

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _location(client: TestClient) -> str:
    described = client.post("/v1/table/b1$t/describe", json={})
    assert described.status_code == 200, described.text
    return described.json()["table_uri"]


def _names(location: str, branch: str | None = None) -> list[str]:
    """The column names actually on disk for the given ref — main when ``branch`` is None."""
    dataset = lance.dataset(location)
    if branch is not None:
        dataset = dataset.checkout_version((branch, None))
    return list(dataset.schema.names)


@pytest.fixture
def branched(real_ns_client: TestClient) -> tuple[TestClient, str]:
    """A real table at ``b1$t`` with a ``dev`` branch, plus its storage location."""
    assert real_ns_client.post("/v1/namespace/b1/create", json={}).status_code == 200
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
    created = real_ns_client.post("/v1/table/b1$t/create?mode=overwrite", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text
    branch = real_ns_client.post("/v1/table/b1$t/branches/create", json={"name": "dev"})
    assert branch.status_code == 200, branch.text
    return real_ns_client, _location(real_ns_client)


def test_add_columns_on_a_branch_lands_on_the_branch_and_not_on_main(branched: tuple[TestClient, str]) -> None:
    client, location = branched
    r = client.post("/v1/table/b1$t/add_columns", json={"branch": "dev", "new_columns": [{"name": "on_dev", "expression": "id + 1"}]})
    assert r.status_code == 200, r.text

    assert "on_dev" in _names(location, "dev")
    assert "on_dev" not in _names(location), "the write landed on main — branch was ignored"
    # The response's version must be the BRANCH's (2, one commit past the branch point), not main's (1).
    # This is the half that silently reverts when only the write is threaded and the read-back is not.
    assert r.json()["version"] == 2, r.text
    assert lance.dataset(location).version == 1


def test_alter_and_drop_on_a_branch_leave_main_alone(branched: tuple[TestClient, str]) -> None:
    """The same threading on the other two schema ops — each one opens its own dataset, so each one is its
    own chance to forget."""
    client, location = branched
    added = client.post("/v1/table/b1$t/add_columns", json={"branch": "dev", "new_columns": [{"name": "tmp", "expression": "id + 1"}]})
    assert added.status_code == 200, added.text

    renamed = client.post("/v1/table/b1$t/alter_columns", json={"branch": "dev", "alterations": [{"path": "tmp", "rename": "kept"}]})
    assert renamed.status_code == 200, renamed.text
    assert "kept" in _names(location, "dev")

    dropped = client.post("/v1/table/b1$t/drop_columns", json={"branch": "dev", "columns": ["v"]})
    assert dropped.status_code == 200, dropped.text
    assert _names(location, "dev") == ["id", "kept"]
    assert _names(location) == ["id", "v"], "main was modified by a branch-targeted op"


def test_field_metadata_on_a_branch_stays_on_the_branch(branched: tuple[TestClient, str]) -> None:
    client, location = branched
    r = client.post("/v1/table/b1$t/update_field_metadata", json={"branch": "dev", "updates": [{"path": "id", "metadata": {"unit": "count"}}]})
    assert r.status_code == 200, r.text

    dev = {f.name: f.metadata for f in lance.dataset(location).checkout_version(("dev", None)).schema}
    main = {f.name: f.metadata for f in lance.dataset(location).schema}
    assert dev["id"] == {b"unit": b"count"}, dev
    assert not main["id"], main


def test_an_unknown_branch_is_a_404_naming_the_branch(branched: tuple[TestClient, str]) -> None:
    """pylance reports a missing branch as a bare ``ValueError`` whose text is a STORAGE PATH
    (``Not found: <root>/tree/nope/_versions``) — unusable twice over: it answers 500, and it publishes the
    bucket layout doing it. The spec has code 22 ``TableBranchNotFound`` for exactly this."""
    client, location = branched
    r = client.post("/v1/table/b1$t/add_columns", json={"branch": "nope", "new_columns": [{"name": "x", "expression": "id + 1"}]})
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["code"] == 22, body  # TABLE_BRANCH_NOT_FOUND
    assert "nope" in str(body["detail"]), body
    assert "/tree/" not in str(body["detail"]), body  # no storage path in a client-facing message
    assert _names(location) == ["id", "v"], "a rejected branch write still touched main"


def test_the_lineage_edge_is_stamped_with_the_branch_version_and_schema(branched: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The trap in this fix: threading ``branch`` into the WRITE and forgetting the lineage read-back.

    ``read_version_and_schema`` reopens the dataset to stamp the WROTE edge; opened on main it reports main's
    (unchanged) version and main's schema, so the graph records the column evolution against a version that
    never carried it — and every assertion about the RESPONSE still passes. Nothing here is stubbed except
    the emit sink itself: the version and the schema are read off the real branch.
    """
    client, _location = branched
    captured: dict[str, object] = {}

    async def _capture(_emitter: object, _segments: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("catalog.api.lineage_deps.emit_write_event", _capture)

    r = client.post("/v1/table/b1$t/add_columns", json={"branch": "dev", "new_columns": [{"name": "on_dev", "expression": "id + 1"}]})
    assert r.status_code == 200, r.text

    assert captured["operation"] == "add_columns"
    assert captured["version"] == 2, captured  # the BRANCH's version — main is still at 1
    names = [field["name"] for field in captured["schema_fields"]]  # ty: ignore[not-iterable]
    assert "on_dev" in names, captured  # read off the branch's schema, not main's


def test_omitting_branch_still_writes_main(branched: tuple[TestClient, str]) -> None:
    """The negative twin: branch is OPTIONAL, and the default target is main. Without this, threading the
    parameter as a required value — or defaulting it to a branch — would pass every test above."""
    client, location = branched
    r = client.post("/v1/table/b1$t/add_columns", json={"new_columns": [{"name": "on_main", "expression": "id * 2"}]})
    assert r.status_code == 200, r.text
    assert "on_main" in _names(location)
    assert "on_main" not in _names(location, "dev")
    assert r.json()["version"] == 2, r.text
