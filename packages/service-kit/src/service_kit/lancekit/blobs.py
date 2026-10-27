"""Blob-v2 column detection — vendored from the pipeline's seam (P2.8 standalone).

A blob-v2 column is identified by the ``lance.blob.v2`` Arrow extension type
(registered when ``lance`` is imported), with the raw ``ARROW:extension:name``
field-metadata fallback. Vendored from a pipeline package that has since been dissolved, so
this is the CANONICAL copy — ``service_kit.lakehouse.blobs`` re-exports it rather than
repeating it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pyarrow as pa


BLOB_V2_EXTENSION_NAME = "lance.blob.v2"


def is_blob_field(field: pa.Field) -> bool:
    """True when ``field`` is a Lance blob-v2 column (file format >= 2.2)."""
    if getattr(field.type, "extension_name", None) == BLOB_V2_EXTENSION_NAME:
        return True
    metadata = field.metadata or {}
    return metadata.get(b"ARROW:extension:name") == BLOB_V2_EXTENSION_NAME.encode()


def nested_fields(field: pa.Field) -> list[pa.Field]:
    """The fields directly beneath ``field`` that a blob-v2 field may be: a Struct's children, a List's or LargeList's item.

    Those are the containers a blob-v2 field may appear beneath (`lance_docs/lance/format/table/blob.md` § Table Schema).
    """
    dtype = field.type
    if pa.types.is_struct(dtype):
        return [dtype.field(i) for i in range(dtype.num_fields)]
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return [dtype.value_field]
    return []


def holds_blob(field: pa.Field) -> bool:
    """True when ``field`` is a blob-v2 field or holds one beneath a Struct, List or LargeList, at any depth."""
    return is_blob_field(field) or any(holds_blob(child) for child in nested_fields(field))


def blob_descriptors(value: object, field: pa.Field) -> Iterator[tuple[str, tuple[str, ...], dict[str, object]]]:
    """``(path, field names, descriptor)`` for each non-null blob descriptor in one scanned value of ``field``.

    ``value`` is the field's value as a descriptor scan's ``to_pylist`` answers it, read along ``field``'s schema rather
    than by its keys, since a user struct may carry a ``kind`` too. ``path`` places the descriptor in the value
    (``page.img``, ``pages[0]``); ``field names`` are the schema's, from the column down (``("pages", "item")``). A null
    container, a null blob and one whose ``kind`` is null hold none (`lance_docs/lance/format/table/blob.md`
    § Null Descriptors).
    """
    yield from _descriptors(value, field, field.name, (field.name,))


def _descriptors(value: object, field: pa.Field, path: str, names: tuple[str, ...]) -> Iterator[tuple[str, tuple[str, ...], dict[str, object]]]:
    if value is None:
        return
    if is_blob_field(field):
        descriptor = cast(dict[str, object], value)
        if descriptor.get("kind") is not None:
            yield path, names, descriptor
        return
    if pa.types.is_struct(field.type):
        struct = cast(dict[str, object], value)
        for child in nested_fields(field):
            if holds_blob(child):
                yield from _descriptors(struct.get(child.name), child, f"{path}.{child.name}", (*names, child.name))
        return
    (item,) = nested_fields(field) or (None,)
    if item is not None:
        for position, element in enumerate(cast(list[object], value)):
            yield from _descriptors(element, item, f"{path}[{position}]", (*names, item.name))


def schema_has_blob(schema: pa.Schema) -> bool:
    """True when any field in ``schema`` is or holds a blob-v2 field."""
    return any(holds_blob(field) for field in schema)


def blob_field_names(schema: pa.Schema) -> list[str]:
    """Names of the blob-v2 columns in ``schema`` (empty when there are none)."""
    return [field.name for field in schema if is_blob_field(field)]
