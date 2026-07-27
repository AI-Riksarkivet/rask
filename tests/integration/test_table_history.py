"""``GET /v1/table/{id}/history`` — the commit log, read out of the format.

The question this answers is the Lakekeeper-console one the owner asked for: *what changed in the data, and
when*. Lance is immutable and append-only at the manifest level, so the answer is already in the dataset —
``versions()`` for the timestamps, the transaction log for the substance — rather than in a side-table the
catalog would have to keep in sync with the data it describes.

Driven end to end through the real ``dir`` backend and real pylance writes, because the whole value of the
endpoint is that it reports what Lance ACTUALLY recorded. A mocked namespace would only pin the shape we
imagined the transaction log has, and the shape is exactly the thing worth verifying — it is why this feature
was scoped from a probe rather than from the docs.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.ipc as ipc
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _seed(client: TestClient) -> None:
    """create → delete → update, so the log has one row of each interesting shape."""
    assert client.post("/v1/namespace/h1/create", json={}).status_code == 200
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
    created = client.post("/v1/table/h1$t/create?mode=overwrite", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text
    deleted = client.post("/v1/table/h1$t/delete", json={"predicate": "id = 2"})
    assert deleted.status_code == 200, deleted.text
    updated = client.post("/v1/table/h1$t/update", json={"updates": [["v", "'Z'"]], "predicate": "id = 3"})
    assert updated.status_code == 200, updated.text


def test_history_reports_what_changed_per_version(real_ns_client: TestClient) -> None:
    _seed(real_ns_client)
    r = real_ns_client.get("/v1/table/h1$t/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == "h1$t"
    rows = body["versions"]

    # Newest first — a log reads backwards, and a UI should not have to re-sort it.
    assert [row["version"] for row in rows] == sorted((row["version"] for row in rows), reverse=True), rows
    assert len(rows) == 3, rows

    by_version = {row["version"]: row for row in rows}
    # v1 = the create. Lance records it as an Overwrite, and it is the version that SET the schema.
    assert by_version[1]["operation"] == "Overwrite"
    assert by_version[1]["schema_set"] is True
    assert by_version[1]["fragments"] == 1

    # v2 = the delete, carrying the predicate AS THE CALLER WROTE IT. This single field is most of why the
    # endpoint is worth having: "rows matching `id = 2` went away" is an answer; "a Delete happened" is not.
    assert by_version[2]["operation"] == "Delete"
    assert by_version[2]["predicate"] == "id = 2"
    assert by_version[2]["schema_set"] is False

    # v3 = the update, with HOW it was applied. Not asserting fields_modified's value: pylance reports 0 for
    # this shape, and pinning a number we do not understand would be pinning a coincidence.
    assert by_version[3]["operation"] == "Update"
    assert by_version[3]["update_mode"] == "rewrite_rows"
    assert "fields_modified" in by_version[3]

    # Every row is timestamped — the WHEN half.
    assert all(row["timestamp"] for row in rows), rows


def test_history_carries_no_actor_and_does_not_pretend_to(real_ns_client: TestClient) -> None:
    """The format has no notion of a user, so this endpoint must not invent one.

    WHO lives in the lineage store's ``author`` run facet, keyed by the same version number
    (``GET /datasets/{name}/producers``). Asserting the absence here is the point: a future change that
    started guessing an actor from, say, the request that happened to read the log would be a fabricated
    audit trail, which is worse than no actor at all.
    """
    _seed(real_ns_client)
    rows = real_ns_client.get("/v1/table/h1$t/history").json()["versions"]
    for row in rows:
        assert "author" not in row
        assert "actor" not in row
        assert "user" not in row


def test_history_limit_bounds_the_transaction_reads(real_ns_client: TestClient) -> None:
    """`limit` exists so a table with many versions cannot turn one UI page into N object-store reads."""
    _seed(real_ns_client)
    rows = real_ns_client.get("/v1/table/h1$t/history?limit=2").json()["versions"]
    assert [row["version"] for row in rows] == [3, 2], rows  # the NEWEST 2, not the first 2


def test_history_of_a_missing_table_is_not_a_500(real_ns_client: TestClient) -> None:
    r = real_ns_client.get("/v1/table/h1$nope/history")
    assert r.status_code in (400, 404), r.text


def test_history_reports_restore_index_and_column_changes(real_ns_client: TestClient) -> None:
    """The operations a create/delete/update whitelist misses — and the one that could corrupt the log.

    Driven against real pylance commits rather than mocks, because every field here was chosen by reading
    ``lance.LanceOperation.<Op>.__dataclass_fields__`` off the installed version. A mock would pin the shape
    we imagined; only a real commit proves the shape Lance writes.

    The Restore assertion is the important one. ``Restore`` carries a field called ``version`` meaning *the
    version whose content was restored*, while the row already has ``version`` meaning *this version*. Copied
    through under its own name it silently overwrites the log's primary key — the row would claim to BE the
    older version, and the lineage actor join would attach the wrong author. So the test asserts both that
    ``restored_from`` is right and that ``version`` was not clobbered.
    """
    _seed(real_ns_client)  # v1 create, v2 delete, v3 update

    added = real_ns_client.post(
        "/v1/table/h1$t/add_columns",
        json={"new_columns": [{"name": "double_id", "expression": "id * 2"}]},
    )
    assert added.status_code == 200, added.text
    dropped = real_ns_client.post("/v1/table/h1$t/drop_columns", json={"columns": ["double_id"]})
    assert dropped.status_code == 200, dropped.text
    indexed = real_ns_client.post("/v1/table/h1$t/create_scalar_index", json={"column": "id", "index_type": "BTREE"})
    assert indexed.status_code == 200, indexed.text
    restored = real_ns_client.post("/v1/table/h1$t/restore", json={"version": 1})
    assert restored.status_code == 200, restored.text

    rows = real_ns_client.get("/v1/table/h1$t/history?limit=50").json()["versions"]
    by_op: dict[str, dict[str, object]] = {}
    for row in rows:
        by_op.setdefault(str(row.get("operation")), row)

    # add_columns commits a Merge, drop_columns commits a Project. Neither carries `new_schema` — only
    # `schema` — so a whitelist testing `new_schema` alone reported schema_set false for a column drop,
    # which is the log denying the change a reader is most likely hunting for.
    assert by_op["Merge"]["schema_set"] is True, by_op["Merge"]
    assert by_op["Project"]["schema_set"] is True, by_op["Project"]

    # An index build must read as more than a bare word.
    assert by_op["CreateIndex"]["new_indices"] == 1, by_op["CreateIndex"]

    # The restore: renamed, present, correct — and the row's own version survived.
    restore_row = by_op["Restore"]
    assert restore_row["restored_from"] == 1, restore_row
    assert restore_row["version"] != 1, f"the row's own version was clobbered by Restore.version: {restore_row}"
    assert restore_row["version"] == max(int(r["version"]) for r in rows), restore_row
