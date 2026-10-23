"""A table's schema in the Lance Namespace spec's JSON form, as the `dir` backend answers it ([[LH-528]]).

DescribeTable with ``load_detailed_metadata`` answers the table's schema as a ``JsonArrowSchema``
(`lance_docs/ns_catalog/spec.yaml` § JsonArrowSchema). The catalog loads it itself, because the backend loads it only
for a table it judges to hold storage, and it misjudges a table registered at a nested location. The conversion is
Lance's own ``arrow_schema_to_json`` (``rust/lance-namespace/src/schema.rs`` at v13.0.0) over the Arrow schema Lance
derives from the table's schema, for every type a Lance table holds: measured on pylance 13.0.0, Lance refuses
``decimal32``, ``decimal64``, ``interval`` and ``union``, so no table holds them, and a type with no form here is refused
rather than answered wrongly.

Each field's metadata is read from Lance's own field, never from pyarrow's. pyarrow turns a registered extension
(``lance.blob.v2``, ``arrow.fixed_shape_tensor``, ``arrow.uuid``) into a type and keeps only its name, while the backend
answers the storage type with ``ARROW:extension:name`` and ``ARROW:extension:metadata`` in the field's metadata, as Lance
stores them (a tensor's ``{"shape":[2,2],"permutation":[0,1]}``). pylance's own ``schema_to_json`` is another format,
which refuses a map.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Protocol, cast

import pyarrow as pa
from lance.schema import LanceSchema
from lance_namespace import InternalError


class _LanceField(Protocol):
    """A field of a Lance schema. ``metadata`` is a getter of Lance's Python binding (``python/src/schema.rs:55`` at
    v13.0.0) that pylance's stub leaves out."""

    @property
    def metadata(self) -> dict[str, str]: ...

    def children(self) -> Sequence[_LanceField]: ...


#: The types the backend names alone, with no child fields and no length.
_NAMED: Final[tuple[tuple[Callable[[pa.DataType], bool], str], ...]] = (
    (pa.types.is_null, "null"),
    (pa.types.is_boolean, "bool"),
    (pa.types.is_int8, "int8"),
    (pa.types.is_uint8, "uint8"),
    (pa.types.is_int16, "int16"),
    (pa.types.is_uint16, "uint16"),
    (pa.types.is_int32, "int32"),
    (pa.types.is_uint32, "uint32"),
    (pa.types.is_int64, "int64"),
    (pa.types.is_uint64, "uint64"),
    (pa.types.is_float16, "float16"),
    (pa.types.is_float32, "float32"),
    (pa.types.is_float64, "float64"),
    (pa.types.is_date32, "date32"),
    (pa.types.is_date64, "date64"),
    (pa.types.is_time32, "time32"),
    (pa.types.is_time64, "time64"),
    (pa.types.is_timestamp, "timestamp"),
    (pa.types.is_duration, "duration"),
    (pa.types.is_string, "utf8"),
    (pa.types.is_large_string, "large_utf8"),
    (pa.types.is_binary, "binary"),
    (pa.types.is_large_binary, "large_binary"),
)

#: The types the backend answers with their child fields: a list's item, a struct's members, a map's entries struct.
_NESTED: Final[tuple[tuple[Callable[[pa.DataType], bool], str], ...]] = (
    (pa.types.is_list, "list"),
    (pa.types.is_large_list, "large_list"),
    (pa.types.is_fixed_size_list, "fixed_size_list"),
    (pa.types.is_struct, "struct"),
    (pa.types.is_map, "map"),
)


def table_schema_json(schema: LanceSchema) -> dict[str, Any]:
    """The table's ``schema`` as the spec's ``JsonArrowSchema``, keyed as on the wire.

    Raises:
        InternalError: A column's type has no JSON form, which no type a Lance table holds lacks.
    """
    arrow = schema.to_pyarrow()
    stored = cast("Sequence[_LanceField]", schema.fields())
    answered: dict[str, Any] = {"fields": [_field_json(arrow.field(index), field) for index, field in enumerate(stored)]}
    if arrow.metadata:
        answered["metadata"] = _text(arrow.metadata)
    return answered


def _field_json(field: pa.Field, stored: _LanceField | None) -> dict[str, Any]:
    """``field``, read with the Lance field it was derived from; a fixed-size list's item has none, and only its metadata."""
    data_type = field.type.storage_type if isinstance(field.type, pa.BaseExtensionType) else field.type
    answered: dict[str, Any] = {"name": field.name, "nullable": field.nullable, "type": _type_json(data_type, stored.children() if stored is not None else ())}
    metadata = stored.metadata if stored is not None else _text(field.metadata or {})
    if metadata:
        answered["metadata"] = dict(metadata)
    return answered


def _type_json(data_type: pa.DataType, children: Sequence[_LanceField]) -> dict[str, Any]:
    if pa.types.is_dictionary(data_type):
        return _type_json(data_type.value_type, children)
    for matches, name in _NAMED:
        if matches(data_type):
            return {"type": name}
    if pa.types.is_decimal128(data_type) or pa.types.is_decimal256(data_type):
        # The backend encodes precision and scale in one number.
        return {"type": f"decimal{data_type.bit_width}", "length": data_type.precision * 1000 + data_type.scale}
    if pa.types.is_fixed_size_binary(data_type):
        return {"type": "fixed_size_binary", "length": data_type.byte_width}
    if pa.types.is_map(data_type) and data_type.keys_sorted:
        raise InternalError(f"a map with sorted keys has no JSON form: {data_type}")
    for matches, name in _NESTED:
        if matches(data_type):
            fields = [_field_json(data_type.field(index), children[index] if index < len(children) else None) for index in range(data_type.num_fields)]
            answered: dict[str, Any] = {"type": name, "fields": fields}
            if pa.types.is_fixed_size_list(data_type):
                answered["length"] = data_type.list_size
            return answered
    raise InternalError(f"the column type {data_type} has no JSON form")


def _text(metadata: Mapping[bytes, bytes]) -> dict[str, str]:
    return {key.decode(): value.decode() for key, value in metadata.items()}
