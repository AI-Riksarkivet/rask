"""A digest of each live row's values, so a ``Rewrite`` is held to what its rows say ([[LH-561]]).

A Rewrite "reorganizes data without semantic modification" (``lance_docs/lance/format/table/transaction.md`` § Rewrite),
which is why a compaction needs only the maintainer rung (ADR 0174). The judge compares the digest of every row a
Rewrite replaces with the digest of the row it writes under the same stable row id.

A row's bytes are an unambiguous encoding of its values, built column by column with Arrow kernels: a validity byte,
then the value. A fixed-width value is its stored bytes (a float bit for bit, so a NaN equals itself and ``-0.0`` is
not ``0.0``), a binary or string value its length then its bytes, a struct its fields in order, a list its length then
its elements. Both sides are read with the table's columns in one order, so the concatenation decodes one way. A blob
is read as its bytes (``blob_handling="all_binary"``), never as its descriptor: measured on pylance 13.0.0, a stock
``compact_files`` moves a blob's descriptor and keeps its bytes. Each row's bytes are hashed with BLAKE2b, so the judge
holds an id and 32 bytes per row, and the rows of one batch at a time.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from lance_namespace import InvalidInputError
from pydantic import BaseModel, ConfigDict, InstanceOf


_DIGEST_SIZE = 32
_DIGEST = np.dtype(f"S{_DIGEST_SIZE}")

#: The rows one scan batch holds: their blobs are read as bytes, so a batch holds this many rows' payloads at once.
_BATCH_ROWS = 256

#: Lance's scan read-ahead, bounded: measured on pylance 13.0.0 over a million 128-float vectors, the scan's defaults held
#: 575 MiB at its peak and these 308 MiB, in the same 0.3 s.
_IO_BUFFER_BYTES = 16 * 1024 * 1024

_VALID = pa.scalar(b"\x01", pa.large_binary())
_NULL = pa.scalar(b"\x00", pa.large_binary())
_EMPTY = pa.scalar(b"", pa.large_binary())


class RowDigests(BaseModel):
    """The stable row id and the digest of each live row of some fragments, ordered by row id."""

    model_config = ConfigDict(frozen=True)

    row_ids: InstanceOf[np.ndarray]
    digests: InstanceOf[np.ndarray]


def of_fragments(dataset: lance.LanceDataset, fragment_ids: Iterable[int], columns: list[str]) -> RowDigests:
    """The digest of every live row of ``dataset``'s fragments ``fragment_ids``, over ``columns`` in that order.

    Raises:
        InvalidInputError: A column holds an Arrow type the encoding does not define.
    """
    wanted = set(fragment_ids)
    fragments = [fragment for fragment in dataset.get_fragments() if fragment.fragment_id in wanted]
    row_ids: list[np.ndarray] = []
    digests: list[np.ndarray] = []
    if fragments:
        scanner = dataset.scanner(
            columns=columns,
            with_row_id=True,
            fragments=fragments,
            blob_handling="all_binary",
            batch_size=_BATCH_ROWS,
            batch_readahead=2,
            fragment_readahead=1,
            io_buffer_size=_IO_BUFFER_BYTES,
        )
        for batch in scanner.to_batches():
            # A copy: a view of the column would keep the whole batch's buffers alive, its blobs and vectors included.
            row_ids.append(batch.column("_rowid").to_numpy(zero_copy_only=False).astype(np.uint64, copy=True))
            rows = _joined([_encoded(batch.column(name)) for name in columns], batch.num_rows)
            packed = b"".join(hashlib.blake2b(row, digest_size=_DIGEST_SIZE).digest() for row in rows.to_pylist())
            digests.append(np.frombuffer(packed, dtype=_DIGEST))
    ids = np.concatenate(row_ids) if row_ids else np.empty(0, np.uint64)
    values = np.concatenate(digests) if digests else np.empty(0, _DIGEST)
    order = np.argsort(ids, kind="stable")
    return RowDigests(row_ids=ids[order], digests=values[order])


def _kernel(name: str, *arguments: pa.Array | pa.Scalar) -> pa.Array:
    """Arrow's compute kernel ``name`` over ``arguments``: pyarrow generates its module functions at import."""
    return pc.call_function(name, list(arguments))


def _joined(parts: list[pa.Array], length: int) -> pa.Array:
    """Each row's parts concatenated, as one large_binary array of ``length`` rows."""
    if not parts:
        return pa.array([b""] * length, pa.large_binary())
    if len(parts) == 1:
        return parts[0]
    return _kernel("binary_join_element_wise", *parts, _EMPTY)


def _fixed(array: pa.Array, width: int) -> pa.Array:
    return array.view(pa.binary(width)).cast(pa.large_binary())


def _encoded(array: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Each slot of ``array`` as its validity byte then its value's bytes: large_binary, with no null."""
    if isinstance(array, pa.ChunkedArray):
        array = array.combine_chunks()
    if isinstance(array, pa.ExtensionArray):
        array = array.storage
    if isinstance(array, pa.DictionaryArray):
        array = array.dictionary_decode()
    valid = _kernel("is_valid", array)
    value = _kernel("if_else", valid, _value_bytes(array), _EMPTY)
    return _kernel("binary_join_element_wise", _kernel("if_else", valid, _VALID, _NULL), value, _EMPTY)


def _value_bytes(array: pa.Array) -> pa.Array:
    kind = array.type
    if pa.types.is_null(kind):
        return pa.nulls(len(array), pa.large_binary())
    if pa.types.is_boolean(kind):
        return _fixed(array.cast(pa.uint8()), 1)
    if pa.types.is_fixed_size_binary(kind):
        return array.cast(pa.large_binary())
    if pa.types.is_string(kind) or pa.types.is_large_string(kind) or pa.types.is_string_view(kind):
        return _length_prefixed(array.cast(pa.large_string()).cast(pa.large_binary()))
    if pa.types.is_binary(kind) or pa.types.is_large_binary(kind) or pa.types.is_binary_view(kind):
        return _length_prefixed(array.cast(pa.large_binary()))
    if pa.types.is_struct(kind):
        return _joined([_encoded(field) for field in array.flatten()], len(array))
    if pa.types.is_fixed_size_list(kind) and _fixed_width(kind.value_type):
        return _fixed_size_list_bytes(array)
    if pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_fixed_size_list(kind) or pa.types.is_map(kind):
        return _list_bytes(array)
    if _fixed_width(kind):
        return _fixed(array, kind.bit_width // 8)
    raise InvalidInputError(f"commit refused: a Rewrite's values cannot be compared on a column of type {kind}")


def _fixed_width(kind: pa.DataType) -> bool:
    """A type each value of which is ``bit_width // 8`` bytes in the array's data buffer (a boolean is a bit).

    Asked of primitives and decimals alone: pyarrow answers ``bit_width`` of any other type with a ValueError.
    """
    if not (pa.types.is_primitive(kind) or pa.types.is_decimal(kind)) or pa.types.is_boolean(kind):
        return False
    return kind.bit_width > 0 and kind.bit_width % 8 == 0


def _fixed_size_list_bytes(array: pa.Array) -> pa.Array:
    """Each list of fixed-width values as one validity byte per element, then the elements' bytes, a null one as zeros.

    The vectors an embedding column holds take this shape: read as whole rows of the data buffer, not element by
    element. Every row has the list's size, so the encoding needs no length.
    """
    kind = array.type
    size, width = kind.list_size, kind.value_type.bit_width // 8
    rows = len(array)
    child = array.values.slice(array.offset * size, rows * size)
    data = np.frombuffer(child.buffers()[1], dtype=np.uint8)[child.offset * width : (child.offset + len(child)) * width].reshape(rows, size * width)
    valid = _kernel("is_valid", child).to_numpy(zero_copy_only=False).reshape(rows, size)
    if not valid.all():
        data = np.where(np.repeat(valid, width, axis=1), data, 0).astype(np.uint8)
    encoded = np.ascontiguousarray(np.concatenate([valid.astype(np.uint8), data], axis=1))
    return pa.Array.from_buffers(pa.binary(size * (width + 1)), rows, [None, pa.py_buffer(encoded.tobytes())]).cast(pa.large_binary())


def _length_prefixed(as_bytes: pa.Array) -> pa.Array:
    return _kernel("binary_join_element_wise", _fixed(_kernel("binary_length", as_bytes).cast(pa.int64()), 8), as_bytes, _EMPTY)


def _list_bytes(array: pa.Array) -> pa.Array:
    """Each list as its length, then each element's encoding: a null list has no element, as an empty one.

    A map is read as the list of key and value structs it is laid out as.
    """
    if pa.types.is_map(array.type):
        array = array.view(pa.list_(pa.struct([array.type.key_field, array.type.item_field])))
    lengths = _kernel("list_value_length", array).fill_null(0).cast(pa.int64())
    elements = _encoded(_kernel("list_flatten", array))
    offsets = pa.array(np.concatenate([[0], np.cumsum(lengths.to_numpy(zero_copy_only=False))]).astype(np.int64))
    per_row = _kernel("binary_join", pa.LargeListArray.from_arrays(offsets, elements), _EMPTY)
    return _kernel("binary_join_element_wise", _fixed(lengths, 8), per_row, _EMPTY)
