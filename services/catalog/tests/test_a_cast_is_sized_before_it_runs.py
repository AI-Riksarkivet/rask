"""XC-104: `bytes_after_cast` bounds what the insert coercion's cast allocates, without running it.

The coercion refuses rows whose cast to the table's types would be over the catalog's body cap, sized by
this estimate before the cast runs. An estimate below what the cast allocates is a way past the cap; one
far above it refuses an honest insert. So each realistic cast is held to both sides here, and the rare
ones (a number written as text, a list view that references one range from many rows) to the first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError, connect

from catalog.services.cast_size import bytes_after_cast
from catalog.services.dataplane import coerce_insert_arrow, create_table
from service_kit.lancekit.arrow_ipc import encode_arrow_stream


if TYPE_CHECKING:
    from pathlib import Path


_ROWS = 10_000
_ONE_LONG_VALUE = pa.DictionaryArray.from_arrays(pa.array([0] * _ROWS, pa.int8()), pa.array(["x" * 1000]))

REALISTIC = [
    pytest.param(_ONE_LONG_VALUE, pa.string(), id="a-dictionary-decoded-once-per-row"),
    pytest.param(
        pa.DictionaryArray.from_arrays(pa.array([0] * (_ROWS - 10) + [1] * 10, pa.int32()), pa.array(["a", "y" * 5000], pa.large_string())),
        pa.string(),
        id="a-dictionary-whose-long-value-is-rare",
    ),
    pytest.param(pa.DictionaryArray.from_arrays(pa.array([0, None] * (_ROWS // 2), pa.int8()), pa.array(["q" * 300])), pa.large_string(), id="null-indices"),
    pytest.param(_ONE_LONG_VALUE.slice(100, 500), pa.string(), id="a-sliced-dictionary"),
    pytest.param(pa.DictionaryArray.from_arrays(pa.array([1] * _ROWS, pa.int8()), pa.array([5, 7], pa.int64())), pa.int64(), id="a-fixed-width-dictionary"),
    pytest.param(pa.array(range(_ROWS), pa.int32()), pa.int64(), id="int32-widened"),
    pytest.param(pa.array([float(i) for i in range(_ROWS)]), pa.int64(), id="the-browsers-float64"),
    pytest.param(pa.array([True] * _ROWS), pa.int64(), id="a-boolean-widened-64-times"),
    pytest.param(pa.nulls(_ROWS), pa.int64(), id="nulls-given-a-width"),
    pytest.param(pa.array(["abc"] * _ROWS), pa.large_string(), id="wider-offsets"),
    pytest.param(
        pa.ListArray.from_arrays(pa.array([0, _ROWS // 2, _ROWS], pa.int32()), _ONE_LONG_VALUE), pa.list_(pa.string()), id="a-dictionary-inside-a-list"
    ),
    pytest.param(pa.StructArray.from_arrays([_ONE_LONG_VALUE], ["d"]), pa.struct([("d", pa.string())]), id="a-dictionary-inside-a-struct"),
]


@pytest.mark.parametrize(("array", "target"), REALISTIC)
def test_a_realistic_cast_is_sized_at_what_it_allocates(array: pa.Array, target: pa.DataType) -> None:
    estimate, allocated = bytes_after_cast(pa.chunked_array([array]), target), array.cast(target).nbytes

    assert estimate >= allocated, f"under-counted: {estimate:,} estimated, {allocated:,} allocated"
    assert estimate <= 1.5 * allocated + 64 * 1024, f"so loose it refuses honest inserts: {estimate:,} estimated, {allocated:,} allocated"


@pytest.mark.parametrize(
    ("array", "target", "allocated"),
    [
        pytest.param(
            pa.array(range(_ROWS), pa.int64()), pa.string(), pa.array(range(_ROWS), pa.int64()).cast(pa.string()).nbytes, id="numbers-written-as-text"
        ),
        pytest.param(
            pa.ListViewArray.from_arrays(pa.array([0] * 100, pa.int32()), pa.array([1000] * 100, pa.int32()), pa.array([1] * 1000, pa.int8())),
            pa.list_(pa.int8()),
            100 * 1000,
            id="a-list-view-referencing-one-range-from-every-row",
        ),
    ],
)
def test_a_rare_cast_is_never_under_counted(array: pa.Array, target: pa.DataType, allocated: int) -> None:
    """`allocated` is what the cast writes: a list view's 100 rows each reference the same 1,000 one-byte elements."""
    assert bytes_after_cast(pa.chunked_array([array]), target) >= allocated


def test_the_coerced_body_is_held_to_the_cap_where_the_estimate_leaves_off(tmp_path: Path) -> None:
    """The estimate counts buffers, not IPC framing: 100 int32 rows widen to an estimated 813 bytes and
    encode to 1,080. At a cap between the two only the encoded body's own check refuses, and without it the
    branch arm, which reads the coerced body again, would refuse what main accepts."""
    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["w"], pa.table({"id": pa.array([0], pa.int64())}), mode="create", registry=None)
    rows = pa.table({"id": pa.array(range(100), pa.int32())})
    body, estimate = encode_arrow_stream(rows), bytes_after_cast(rows.column("id"), pa.int64())
    encoded = len(encode_arrow_stream(rows.cast(pa.schema([("id", pa.int64())]))))
    cap = (max(len(body), estimate) + encoded) // 2
    assert max(len(body), estimate) < cap < encoded, "no cap separates the estimate from the encoded body, so this tests nothing"

    with pytest.raises(InvalidInputError, match="as the table's column types"):
        coerce_insert_arrow(ns, {}, ["w"], body, max_bytes=cap)
