"""The column ops answer a CLIENT mistake with the spec's own 4xx — never a 500 (#101).

Seven distinct user errors on ``add_columns`` / ``alter_columns`` / ``drop_columns`` /
``update_field_metadata`` used to answer ``500 InternalError`` with detail ``"Internal Server Error"``:
``dataplane._user_sql`` existed for exactly this class of failure and had only ever been wired to
``update``/``delete``. The lakehouse UI renders the problem body's ``detail`` verbatim, so a user who typed
a column name that does not exist read that the server had broken.

Driven against the REAL ``dir`` backend and real pylance writes, because the thing under test is how
**Lance** reports each mistake — its exception CLASS is not consistent (``drop_columns`` raises an unmarked
``ValueError``, ``alter_columns`` an ``OSError`` carrying the ``Invalid user input`` marker), which is the
precise reason the first translator missed half of them. A mocked namespace could only pin the messages we
imagined. It also means a pylance bump that rewords a message reds a test here instead of silently
returning these paths to 500s.
"""

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
#: Lance appends the source location of the Rust frame that raised. It is the build path of a vendored
#: library and must never reach a caller.
RUST_PATH_FRAGMENT = ".rs:"


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture
def table(real_ns_client: TestClient) -> TestClient:
    """A real two-column table at ``c1$t`` — ``id: int64``, ``v: string``."""
    assert real_ns_client.post("/v1/namespace/c1/create", json={}).status_code == 200
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
    created = real_ns_client.post("/v1/table/c1$t/create?mode=overwrite", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text
    return real_ns_client


def _problem(response: object) -> dict[str, object]:
    """The RFC 9457 body, asserted to BE one — a bare FastAPI error body would pass a status check."""
    body = response.json()  # ty: ignore[unresolved-attribute]
    assert response.headers["content-type"].startswith("application/problem+json"), body  # ty: ignore[unresolved-attribute]
    assert set(body) >= {"type", "title", "status", "detail", "code"}, body
    return body


def test_add_columns_with_an_unknown_field_in_the_expression_is_a_400(table: TestClient) -> None:
    """The expression is the caller's, and Lance's rejection NAMES the fields that do exist — the single
    most useful sentence in the response. It is invalid input, not a missing column: nothing was targeted
    by name, an expression failed to resolve."""
    r = table.post("/v1/table/c1$t/add_columns", json={"new_columns": [{"name": "x", "expression": "nosuchcol + 1"}]})
    assert r.status_code == 400, r.text
    body = _problem(r)
    assert body["code"] == 13, body  # INVALID_INPUT
    detail = str(body["detail"])
    assert "nosuchcol" in detail, detail
    assert "Valid fields are" in detail, detail
    assert RUST_PATH_FRAGMENT not in detail, detail


def test_add_columns_with_a_name_the_table_already_has_is_a_400(table: TestClient) -> None:
    """A duplicate column is a conflicting REQUEST, not a server fault."""
    r = table.post("/v1/table/c1$t/add_columns", json={"new_columns": [{"name": "id", "expression": "id + 1"}]})
    assert r.status_code == 400, r.text
    body = _problem(r)
    assert body["code"] == 13, body
    assert "already exists" in str(body["detail"]), body


def test_drop_columns_of_a_missing_column_is_a_404_column_not_found(table: TestClient) -> None:
    """A column named by the caller that is not there is exactly ``TableColumnNotFound`` (code 12) — the
    spec has a code for it, so the answer must carry that code, not a 400 and certainly not a 500. Lance
    raises this one as an UNMARKED ``ValueError``: the marker-only translator could never have caught it."""
    r = table.post("/v1/table/c1$t/drop_columns", json={"columns": ["nope"]})
    assert r.status_code == 404, r.text
    body = _problem(r)
    assert body["code"] == 12, body  # TABLE_COLUMN_NOT_FOUND
    detail = str(body["detail"])
    assert "nope" in detail, detail
    # The answer names what IS there — Lance's own message for this path does not, so the catalog appends it.
    assert "id" in detail and "v" in detail, detail


def test_dropping_every_column_is_a_400(table: TestClient) -> None:
    """A table with no columns is not a thing Lance will make. The columns all exist, so this is not a
    not-found — it is a refusal of a well-formed but impossible request."""
    r = table.post("/v1/table/c1$t/drop_columns", json={"columns": ["id", "v"]})
    assert r.status_code == 400, r.text
    assert _problem(r)["code"] == 13


def test_renaming_a_missing_column_is_a_404_column_not_found(table: TestClient) -> None:
    """Same class as the drop, reported by Lance in a different shape (``OSError`` WITH the marker) — both
    must land on the same code, or a client cannot dispatch on it."""
    r = table.post("/v1/table/c1$t/alter_columns", json={"alterations": [{"path": "nope", "rename": "x"}]})
    assert r.status_code == 404, r.text
    body = _problem(r)
    assert body["code"] == 12, body
    assert "nope" in str(body["detail"]), body


def test_an_impossible_cast_is_a_400_that_names_the_cast(table: TestClient) -> None:
    """The column exists; the CAST is what cannot be done. The message names both types, which is what
    tells the caller whether to pick a different target type or rewrite the column."""
    r = table.post("/v1/table/c1$t/alter_columns", json={"alterations": [{"path": "id", "data_type": {"type": "string"}}]})
    assert r.status_code == 400, r.text
    body = _problem(r)
    assert body["code"] == 13, body
    detail = str(body["detail"])
    assert "Int64" in detail and "Utf8" in detail, detail
    assert RUST_PATH_FRAGMENT not in detail, detail


def test_field_metadata_on_an_unknown_path_is_a_404_column_not_found(table: TestClient) -> None:
    """``update_field_metadata`` targets a field BY PATH, so an unknown path is the same 404. Lance's own
    message already enumerates the available fields — the catalog must not append a second copy."""
    r = table.post("/v1/table/c1$t/update_field_metadata", json={"updates": [{"path": "nope", "metadata": {"a": "b"}}]})
    assert r.status_code == 404, r.text
    body = _problem(r)
    assert body["code"] == 12, body
    detail = str(body["detail"])
    assert "nope" in detail, detail
    assert detail.count("id") == 1, detail  # listed once, by Lance — not appended a second time


def test_a_valid_column_op_still_succeeds(table: TestClient) -> None:
    """The negative twin: the translation must not turn a GOOD request into a 4xx. Without this, a
    contextmanager that raised unconditionally would pass all seven tests above."""
    r = table.post("/v1/table/c1$t/add_columns", json={"new_columns": [{"name": "double_id", "expression": "id * 2"}]})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2, r.text


def test_backfill_tells_the_caller_what_is_unsupported(table: TestClient) -> None:
    """``backfill_columns`` is a genuine native stub, and Unsupported is the HONEST answer — but ``problem_detail``
    redacted every ``>= 500`` detail, so the one sentence the caller needs ("this backend does not implement
    it") was replaced with "Internal Server Error". The lakehouse ships a backfill button, so a user pressed
    it and was told the server had broken. It names an operation and leaks nothing; it is not redacted.
    406 since Q3 (2026-09-02) — the spec's status for Unsupported, which also stops a capability answer
    being counted as a server fault in the RED metrics."""
    r = table.post("/v1/table/c1$t/backfill_column", json={"column": "id"})
    assert r.status_code == 406, r.text
    body = _problem(r)
    assert body["code"] == 0, body  # UNSUPPORTED
    detail = str(body["detail"])
    assert detail != "Internal Server Error", body
    assert "backfill" in detail.lower(), detail
    assert body["error"] == detail, body  # the spec-0.9 twin field carries the same text
