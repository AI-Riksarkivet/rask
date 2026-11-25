"""#64 insert robustness: the catalog casts Arrow-IPC insert rows to the table schema before the append.

A browser's apache-arrow infers float64 for every JS number, so an insert into an int64 column otherwise
500s at the native append (browser-driven find 2026-07-21). ``coerce_insert_arrow`` casts float64→int64 and
turns a genuinely incompatible payload into a clean 400 (InvalidInputError), never a 500.
"""

from typing import Any, ClassVar, cast
from unittest.mock import create_autospec

import lance
import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError

from catalog.services.tables import writes
from lakehouse.arrow_ipc import encode_arrow_stream
from tests.door_tables import BODY_LIMIT


class _Table:
    """The door's table, answering the schema the coercion aligns to: the only thing the coercion reads of it."""

    segments: ClassVar[list[str]] = ["hunt", "t"]

    def __init__(self, schema: pa.Schema) -> None:
        self._schema = schema

    def open(self, so: Any, *, version: int | None = None, branch: str | None = None) -> lance.LanceDataset:  # noqa: ANN401 — the store options are not read
        """A handle with the dataset's whole signature (``create_autospec``) whose schema is the table's."""
        handle = create_autospec(lance.LanceDataset, instance=True)
        handle.schema = self._schema
        return handle


def _coerce(table: _Table, data: bytes) -> pa.Table:
    out = writes.coerce_insert_arrow(cast(Any, table), cast(Any, {}), data, max_bytes=BODY_LIMIT)
    return pa.ipc.open_stream(out).read_all()


def test_reorders_columns_by_name() -> None:
    table = _Table(pa.schema([("id", pa.int64()), ("tag", pa.string())]))
    # columns out of order → aligned by name and cast to the table's types
    incoming = pa.table(
        {
            "tag": pa.array(["a"], pa.string()),
            "id": pa.array([1.0], pa.float64()),
        }
    )
    out = _coerce(table, encode_arrow_stream(incoming))
    assert out.schema.names == ["id", "tag"]
    assert out.column("id").to_pylist() == [1]


def test_non_integral_value_is_400_not_500() -> None:
    table = _Table(pa.schema([("id", pa.int64())]))
    incoming = pa.table({"id": pa.array([4.5], pa.float64())})  # 4.5 can't become an int
    with pytest.raises(InvalidInputError):
        _coerce(table, encode_arrow_stream(incoming))


def test_missing_column_is_400() -> None:
    table = _Table(pa.schema([("id", pa.int64()), ("score", pa.int64())]))
    incoming = pa.table({"id": pa.array([1], pa.int64())})  # no 'score'
    with pytest.raises(InvalidInputError):
        _coerce(table, encode_arrow_stream(incoming))


def test_matching_names_with_differing_nullability_still_coerces() -> None:
    """Same names+types but a non-nullable target field ⇒ NOT the fast path — the cast must run so
    the declared nullability lands (the native append checks the schema, not the values)."""
    table = _Table(pa.schema([pa.field("id", pa.int64(), nullable=False)]))
    data = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))  # incoming field is nullable=True
    out = writes.coerce_insert_arrow(cast(Any, table), cast(Any, {}), data, max_bytes=BODY_LIMIT)
    assert out is not data
    assert pa.ipc.open_stream(out).read_all().schema.field("id").nullable is False
