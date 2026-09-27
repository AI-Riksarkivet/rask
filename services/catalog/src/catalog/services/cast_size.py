"""What casting insert rows to a table's own column types allocates, computed before any cast runs.

`dataplane.coerce_insert_arrow` casts a caller's rows to the table's types. Most casts grow a column by a
bounded width ratio, but one that decodes a dictionary writes its value out once per row that references
it: measured on pyarrow 25.0.0, a 656-byte insert whose million rows reference one 1,000-byte value cast
to 1,004,000,296 bytes. So the cast is sized first, from how often each dictionary value is referenced and
the widths the target types store, and refused over the door's cap before it allocates anything.

Every figure is an upper bound, so a cast that fits is never refused for a miscount in its favour.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc


#: The longest text a cast writes for one fixed-width value: a decimal256 or a zoned timestamp is shorter.
_TEXT_BYTES_PER_VALUE = 96
#: A variable-width result's offsets, sized at the wider of the two offset widths.
_OFFSET_BYTES = 8


def bytes_after_cast(column: pa.ChunkedArray, target: pa.DataType) -> int:
    """An upper bound on what casting ``column`` to ``target`` allocates, computed without casting it."""
    return sum(_array_bytes(chunk, target) for chunk in column.chunks)


def _array_bytes(array: pa.Array, target: pa.DataType) -> int:
    rows = len(array)
    validity = (rows + 7) // 8
    source = array.type
    if pa.types.is_dictionary(source) and not pa.types.is_dictionary(target):
        return validity + _gathered_bytes(array, target)
    if pa.types.is_dictionary(target) or pa.types.is_null(target):
        # Encoding into a dictionary holds at most the values it came from plus one index per row.
        return 0 if pa.types.is_null(target) else array.nbytes + _OFFSET_BYTES * rows
    if _is_fixed_width(target):
        return validity + (rows * target.bit_width + 7) // 8
    offsets = _OFFSET_BYTES * (rows + 1)
    if _is_binary_like(target):
        return validity + offsets + (array.nbytes if _is_binary_like(source) else rows * _TEXT_BYTES_PER_VALUE)
    if pa.types.is_struct(target) and isinstance(array, pa.StructArray):
        names = {field.name for field in source}
        return validity + sum(_array_bytes(array.field(field.name), field.type) for field in target if field.name in names)
    if pa.types.is_map(target) and isinstance(array, pa.MapArray):
        return validity + offsets + _array_bytes(array.keys, target.key_type) + _array_bytes(array.items, target.item_type)
    if _is_list_like(target) and _is_list_like(source):
        return validity + offsets + _elements_bytes(array, target.value_type)
    return array.nbytes


def _elements_bytes(array: pa.Array, value_type: pa.DataType) -> int:
    """A list's elements as ``value_type``. A list view may reference one element range from many rows, so
    its elements are counted by their sizes, each at the most any one element costs."""
    if pa.types.is_list_view(array.type) or pa.types.is_large_list_view(array.type):
        referenced = _call("sum", _call("list_value_length", array)).as_py() or 0
        return referenced * _widest(array.values, value_type) if len(array.values) else 0
    return _array_bytes(array.flatten(), value_type)


def _widest(values: pa.Array, target: pa.DataType) -> int:
    """The most any one of ``values`` costs as ``target``: exact for a fixed width or text, and the whole
    array otherwise, which over-counts a nested value rather than walk a caller-sized array in Python."""
    if _is_fixed_width(target):
        return (target.bit_width + 7) // 8 + 1
    if _is_binary_like(target) and _is_binary_like(values.type):
        return (_call("max", _call("binary_length", values)).as_py() or 0) + _OFFSET_BYTES + 1
    return _array_bytes(values, target)


def _gathered_bytes(array: pa.DictionaryArray, target: pa.DataType) -> int:
    """A dictionary decoded to ``target``: each referenced value's own size, once per row that references it."""
    rows = len(array)
    if _is_fixed_width(target):
        return (rows * target.bit_width + 7) // 8
    counts = array.indices.value_counts()
    present = counts.field("values").is_valid()
    referenced, times = counts.field("values").filter(present), counts.field("counts").filter(present)
    values = array.dictionary
    if _is_binary_like(target) and _is_binary_like(values.type):
        lengths = _call("binary_length", values).cast(pa.int64()).take(referenced)
        return _OFFSET_BYTES * (rows + 1) + (_call("sum", _call("multiply", lengths, times)).as_py() or 0)
    return rows * _widest(values, target)


def _call(name: str, *arguments: pa.Array) -> pa.Array | pa.Scalar:
    """A pyarrow compute function by name: the module's generated functions are invisible to the type checker."""
    return pc.call_function(name, list(arguments))


def _is_fixed_width(data_type: pa.DataType) -> bool:
    return (
        pa.types.is_boolean(data_type)
        or pa.types.is_integer(data_type)
        or pa.types.is_floating(data_type)
        or pa.types.is_decimal(data_type)
        or pa.types.is_temporal(data_type)
        or pa.types.is_fixed_size_binary(data_type)
    )


def _is_binary_like(data_type: pa.DataType) -> bool:
    return (
        pa.types.is_string(data_type)
        or pa.types.is_large_string(data_type)
        or pa.types.is_binary(data_type)
        or pa.types.is_large_binary(data_type)
        or pa.types.is_string_view(data_type)
        or pa.types.is_binary_view(data_type)
    )


def _is_list_like(data_type: pa.DataType) -> bool:
    return (
        pa.types.is_list(data_type)
        or pa.types.is_large_list(data_type)
        or pa.types.is_fixed_size_list(data_type)
        or pa.types.is_list_view(data_type)
        or pa.types.is_large_list_view(data_type)
    )
