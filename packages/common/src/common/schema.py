"""Render an Arrow/Lance schema into OpenLineage ``SchemaDatasetFacet`` fields.

A blob-v2 column is labelled ``"blob"`` (not its verbose extension storage type), a fixed-size vector as
``"array<elem>"``, binary as ``"binary"``, and a JSON column as ``"json"`` — so the lineage graph shows
meaningful column types for media tables (``payload : blob``, ``embedding : array<float>``,
``thumbnail : binary``, ``alignments_json : json``) instead of the raw pyarrow repr. Shared by the
medallion emitter (WROTE-edge schema facet) and the lineage demo peek.
"""

from __future__ import annotations

import pyarrow as pa

from common.blobs import is_blob_field


#: Extension names a JSON column can carry. ``pa.json_()`` reads back as ``arrow.json``; Lance's own
#: docs name the storage extension ``lance.json`` (it stores the value as JSONB either way). pyarrow 24
#: has no ``pa.types.is_json`` predicate, so match the extension name directly.
_JSON_EXTENSION_NAMES = frozenset({"arrow.json", "lance.json"})

#: The ``fields`` of an OpenLineage ``SchemaDatasetFacet`` — one ``{"name", "type"}`` (optionally
#: ``"description"``) dict per column, in wire-JSON form. A shared alias so the catalog + medallion emitters
#: and this renderer name the same shape once instead of repeating ``list[dict[str, str]]`` across signatures.
type SchemaFields = list[dict[str, str]]


def type_label(field: pa.Field) -> str:
    """A concise lineage type label — blob/vector/binary/json specialised, else the pyarrow repr."""
    if is_blob_field(field):
        return "blob"
    dtype = field.type
    if getattr(dtype, "extension_name", None) in _JSON_EXTENSION_NAMES:
        return "json"
    if pa.types.is_fixed_size_list(dtype) or pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return f"array<{dtype.value_type}>"
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype):
        return "binary"
    return str(dtype)


def facet_fields(schema: pa.Schema) -> SchemaFields:
    """The ``fields`` of an OpenLineage ``SchemaDatasetFacet`` — ``[{"name", "type"}]`` per column."""
    return [{"name": field.name, "type": type_label(field)} for field in schema]
