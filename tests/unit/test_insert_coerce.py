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
from catalog.services import dataplane
from lance_namespace import InvalidInputError


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()


def _target(monkeypatch: pytest.MonkeyPatch, schema: pa.Schema) -> None:
    monkeypatch.setattr(dataplane, "open_dataset", lambda *_a, **_k: SimpleNamespace(schema=schema))


def _coerce(data: bytes) -> pa.Table:
    out = dataplane.coerce_insert_arrow(cast(Any, None), cast(Any, {}), ["hunt", "t"], data)
    return pa.ipc.open_stream(out).read_all()


def test_casts_browser_float64_to_int64(monkeypatch: pytest.MonkeyPatch) -> None:
    _target(
        monkeypatch,
        pa.schema([("id", pa.int64()), ("score", pa.int64()), ("tag", pa.string())]),
    )
    incoming = pa.table(
        {
            "id": pa.array([4.0], pa.float64()),  # what a browser infers for a JS number
            "score": pa.array([40.0], pa.float64()),
            "tag": pa.array(["d"], pa.string()),
        }
    )
    out = _coerce(_ipc(incoming))
    assert out.schema.field("id").type == pa.int64()
    assert out.column("id").to_pylist() == [4]
    assert out.column("score").to_pylist() == [40]


def test_reorders_and_drops_extra_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    _target(monkeypatch, pa.schema([("id", pa.int64()), ("tag", pa.string())]))
    # columns out of order + an extra 'junk' the table doesn't have → aligned by name, extra dropped
    incoming = pa.table(
        {
            "tag": pa.array(["a"], pa.string()),
            "junk": pa.array([9], pa.int64()),
            "id": pa.array([1.0], pa.float64()),
        }
    )
    out = _coerce(_ipc(incoming))
    assert out.schema.names == ["id", "tag"]
    assert out.column("id").to_pylist() == [1]


def test_non_integral_value_is_400_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _target(monkeypatch, pa.schema([("id", pa.int64())]))
    incoming = pa.table({"id": pa.array([4.5], pa.float64())})  # 4.5 can't become an int
    with pytest.raises(InvalidInputError):
        _coerce(_ipc(incoming))


def test_missing_column_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _target(monkeypatch, pa.schema([("id", pa.int64()), ("score", pa.int64())]))
    incoming = pa.table({"id": pa.array([1], pa.int64())})  # no 'score'
    with pytest.raises(InvalidInputError):
        _coerce(_ipc(incoming))
