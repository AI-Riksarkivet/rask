"""``_column_op`` classifies CLIENT mistakes and nothing else (#101).

The HTTP side of this contract is `tests/integration/test_column_errors.py`, driven through the real
backend. This file pins the other half — the failures that must NOT be reclassified — because they cannot
be provoked through the real store: the whole point is what happens when the OBJECT STORE fails, not the
request.

The discipline is an allowlist, not `except Exception`. `_user_sql` could key on Lance's single
``Invalid user input`` marker; `_column_op` cannot, because `drop_columns` reports both of its user errors
as UNMARKED ``ValueError``s — so the match had to widen, and widening is exactly how a translator starts
blaming callers for outages.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from catalog.services.dataplane import add_columns, create_table, drop_columns
from lance_namespace import (
    AlterTableAddColumnsRequest,
    AlterTableDropColumnsRequest,
    LanceNamespace,
    LanceNamespaceError,
    connect,
)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def table(tmp_path: Path) -> LanceNamespace:
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["t"], _ipc(pa.table({"id": [1, 2, 3], "v": ["a", "b", "c"]})), mode="create")
    return ns


def test_a_storage_failure_on_a_column_op_is_not_blamed_on_the_caller(table: LanceNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` with no user-error signature propagates untranslated — it is a 500, and it stays one.

    ``drop_columns``' real user errors arrive as bare ``ValueError``/``OSError`` with no marker, so the
    translator matches on MESSAGE. The failure mode of that design is swallowing the class: every write
    would answer "fix your columns" while the object store was down.
    """

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("object store unreachable")

    monkeypatch.setattr("catalog.services.dataplane.open_dataset", boom)
    with pytest.raises(OSError, match="unreachable") as caught:
        drop_columns(table, {}, AlterTableDropColumnsRequest(id=["t"], columns=["v"]))
    assert not isinstance(caught.value, LanceNamespaceError), "a storage outage was minted into a domain error"


def test_a_typed_domain_error_passes_through_unchanged(table: LanceNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error that is ALREADY the spec's own (``TableBranchNotFound`` out of ``open_dataset``, our own
    ``InvalidInput`` for an unsupported cast target) must not be re-wrapped: re-wrapping would restate a 404
    as a 400 and lose the code clients dispatch on."""
    from lance_namespace import TableBranchNotFoundError

    def missing_branch(*_a: object, **_k: object) -> None:
        raise TableBranchNotFoundError("branch 'dev' not found on table t")

    monkeypatch.setattr("catalog.services.dataplane.open_dataset", missing_branch)
    with pytest.raises(TableBranchNotFoundError) as caught:
        add_columns(table, {}, AlterTableAddColumnsRequest(id=["t"], branch="dev", new_columns=[{"name": "x", "expression": "id + 1"}]))
    assert int(caught.value.code) == 22
