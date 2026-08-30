"""A caller's bad SQL expression is a 4xx, not a 500.

``updates`` and ``predicate`` on the row-mutation endpoints are SQL fragments the CALLER writes, and Lance
validates them at execution time, raising ``Invalid user input: Schema error: No field named "Z". Valid
fields are id, v.`` — which escaped uncaught and became a 500 ``InternalError``. Found live
2026-07-26 driving ``POST /v1/table/{id}/update`` with an unquoted string literal (Lance reads that as a
column reference), against the deployed catalog on kind.

That is wrong three ways: it tells the caller the server broke when their expression was wrong, it buries
Lance's genuinely useful message (it lists the valid fields) under a generic body, and it spends the error
budget — a 500 is what alerting watches — on a client mistake.

Real dir namespace + real pylance, no mocks: the point is Lance's ACTUAL error, and a mock would only pin
the message we imagined it raises. That mattered here — writing these tests is what showed the exception
CLASS is inconsistent (``ValueError`` from ``update()``, ``OSError`` from ``delete()``, same message), so a
type-keyed guard would have fixed update and left delete returning 500s.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance_namespace import (
    DeleteFromTableRequest,
    InvalidInputError,
    LanceNamespace,
    UpdateTableRequest,
    connect,
)

from catalog.services.dataplane import create_table, delete_from_table, update_table


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def table(tmp_path: Path) -> LanceNamespace:
    """A two-column table (`id`, `v`) in a real dir namespace."""
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["t"], _ipc(pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]})), mode="create")
    return ns


def test_update_with_an_unknown_column_is_invalid_input(table: LanceNamespace) -> None:
    # The live shape of the bug: `Z` unquoted is a column reference, and there is no column Z.
    with pytest.raises(InvalidInputError) as caught:
        update_table(table, {}, UpdateTableRequest(id=["t"], updates=[["v", "Z"]], predicate="id = 3"))
    message = str(caught.value)
    assert "invalid update expression" in message
    # Lance's own text is preserved — it names the columns that DO exist, which is the whole value of it.
    assert "No field named" in message
    assert "id, v" in message
    # And the "Invalid user input: " prefix is stripped rather than parroted into our message.
    assert "Invalid user input" not in message
    # Lance appends the Rust source location that raised (".../rust/lance/src/.../update.rs:150:14"). The
    # caller can do nothing with it and it publishes a vendored library's build path in an API response, so
    # it is trimmed from the detail — the log line keeps the original.
    assert ".rs:" not in message


def test_update_with_a_bad_predicate_is_invalid_input(table: LanceNamespace) -> None:
    with pytest.raises(InvalidInputError):
        update_table(table, {}, UpdateTableRequest(id=["t"], updates=[["v", "'Z'"]], predicate="nosuchcol = 3"))


def test_delete_with_a_bad_predicate_is_invalid_input(table: LanceNamespace) -> None:
    with pytest.raises(InvalidInputError):
        delete_from_table(table, {}, DeleteFromTableRequest(id=["t"], predicate="nosuchcol = 3"))


def test_a_valid_update_still_works(table: LanceNamespace) -> None:
    # The guard must not swallow the happy path: a correctly quoted literal updates one row and bumps the
    # version. Without this the tests above would pass just as well against a function that always raised.
    response = update_table(table, {}, UpdateTableRequest(id=["t"], updates=[["v", "'Z'"]], predicate="id = 3"))
    assert response.updated_rows == 1
    assert response.version == 2


def test_a_storage_failure_is_not_reclassified(table: LanceNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage/IO failure stays a 5xx.

    The guard discriminates on Lance's "Invalid user input" MARKER, not on the exception class — it has to,
    because the same bad predicate is a ValueError out of update() and an OSError out of delete(). This is
    the other side of that: an OSError WITHOUT the marker must propagate. Widening the guard to swallow the
    class would turn every genuine outage on the write path into "fix your query" while the object store
    was down.
    """

    def boom(*_a: object, **_k: object) -> None:
        # NOTE: an OSError, the same class Lance's delete() raises for a bad predicate — but WITHOUT the
        # "Invalid user input" marker. That is exactly the pair the guard has to tell apart.
        raise OSError("object store unreachable")

    monkeypatch.setattr("catalog.services.dataplane.open_dataset", boom)
    with pytest.raises(OSError, match="unreachable"):
        update_table(table, {}, UpdateTableRequest(id=["t"], updates=[["v", "'Z'"]], predicate="id = 3"))
