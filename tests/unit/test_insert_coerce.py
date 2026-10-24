"""#64 insert robustness: the catalog casts Arrow-IPC insert rows to the table schema before the append.

A browser's apache-arrow infers float64 for every JS number, so an insert into an int64 column otherwise
500s at the native append (browser-driven find 2026-07-21). ``coerce_insert_arrow`` casts float64→int64 and
turns a genuinely incompatible payload into a clean 400 (InvalidInputError), never a 500.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError

from catalog.services import dataplane


#: The catalog's default body cap (LANCE_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 256 * 1024 * 1024


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()


class _Table:
    """The door's table, answering the schema the coercion aligns to: the only thing the coercion reads of it."""

    segments = ["hunt", "t"]

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema

    def open(self, so: Any, *, version: int | None = None, branch: str | None = None) -> Any:  # noqa: ANN401 — a handle's schema is all that is read
        return SimpleNamespace(schema=self._schema)


def _coerce(table: _Table, data: bytes) -> pa.Table:
    out = dataplane.coerce_insert_arrow(cast(Any, table), cast(Any, {}), data, max_bytes=_BODY_LIMIT)
    return pa.ipc.open_stream(out).read_all()


def test_reorders_and_drops_extra_columns() -> None:
    table = _Table(pa.schema([("id", pa.int64()), ("tag", pa.string())]))
    # columns out of order + an extra 'junk' the table doesn't have → aligned by name, extra dropped
    incoming = pa.table(
        {
            "tag": pa.array(["a"], pa.string()),
            "junk": pa.array([9], pa.int64()),
            "id": pa.array([1.0], pa.float64()),
        }
    )
    out = _coerce(table, _ipc(incoming))
    assert out.schema.names == ["id", "tag"]
    assert out.column("id").to_pylist() == [1]


def test_non_integral_value_is_400_not_500() -> None:
    table = _Table(pa.schema([("id", pa.int64())]))
    incoming = pa.table({"id": pa.array([4.5], pa.float64())})  # 4.5 can't become an int
    with pytest.raises(InvalidInputError):
        _coerce(table, _ipc(incoming))


def test_missing_column_is_400() -> None:
    table = _Table(pa.schema([("id", pa.int64()), ("score", pa.int64())]))
    incoming = pa.table({"id": pa.array([1], pa.int64())})  # no 'score'
    with pytest.raises(InvalidInputError):
        _coerce(table, _ipc(incoming))


def test_matching_names_with_differing_nullability_still_coerces() -> None:
    """Same names+types but a non-nullable target field ⇒ NOT the fast path — the cast must run so
    the declared nullability lands (the native append checks the schema, not the values)."""
    table = _Table(pa.schema([pa.field("id", pa.int64(), nullable=False)]))
    data = _ipc(pa.table({"id": pa.array([1], pa.int64())}))  # incoming field is nullable=True
    out = dataplane.coerce_insert_arrow(cast(Any, table), cast(Any, {}), data, max_bytes=_BODY_LIMIT)
    assert out is not data
    assert pa.ipc.open_stream(out).read_all().schema.field("id").nullable is False
