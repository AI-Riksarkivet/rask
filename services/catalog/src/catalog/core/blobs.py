"""Blob-v2 columns in the catalog: detection, their scanned descriptors, and the aligned read.

Lance blob-v2 columns require file format ``>= 2.2`` and are identified by the
``lance.blob.v2`` Arrow extension type (registered when ``lance`` is imported), with the raw
``ARROW:extension:name`` field-metadata fallback. These helpers recognise a blob column from an Arrow
schema without materialising the payloads; blob serving, the create path, erasure and the base checks use
them. One module answers "which Arrow extension name marks a blob column", so the answer has one place
to change.

Detection picks no storage version: no writer chooses one from the schema, and every create in
``services/``, ``packages/`` and ``scripts/`` names ``data_storage_version="2.2"`` (grep, 2026-09-25).

:func:`read_aligned_table` is the READ counterpart: one scan whose blob columns arrive as bytes,
row-aligned with the tabular columns and with a null payload as ``None`` on its own row.
"""

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pyarrow as pa


__all__ = [
    "BLOB_V2_EXTENSION_NAME",
    "EXTERNAL_KIND",
    "blob_descriptors",
    "blob_field_names",
    "carry_external_descriptor",
    "holds_blob",
    "is_blob_field",
    "nested_fields",
    "read_aligned_table",
    "schema_has_blob",
]

if TYPE_CHECKING:
    import lance

log = logging.getLogger(__name__)

#: Arrow extension name Lance stamps on a blob-v2 column (lance_docs/lance/guide/blob.md — Version Compatibility).
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
    yield from _descriptors(value, field, field.name, names=(field.name,))


def _descriptors(value: object, field: pa.Field, path: str, *, names: tuple[str, ...]) -> Iterator[tuple[str, tuple[str, ...], dict[str, object]]]:
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
                yield from _descriptors(struct.get(child.name), child, f"{path}.{child.name}", names=(*names, child.name))
        return
    (item,) = nested_fields(field) or (None,)
    if item is not None:
        for position, element in enumerate(cast(list[object], value)):
            yield from _descriptors(element, item, f"{path}[{position}]", names=(*names, item.name))


def schema_has_blob(schema: pa.Schema) -> bool:
    """True when any field in ``schema`` is or holds a blob-v2 field."""
    return any(holds_blob(field) for field in schema)


def blob_field_names(schema: pa.Schema) -> list[str]:
    """Names of the blob-v2 columns in ``schema`` (empty when there are none)."""
    return [field.name for field in schema if is_blob_field(field)]


#: The descriptor `kind` for an EXTERNAL blob — the payload lives at a URI the dataset does not own.
#: Measured, not documented: 0 inline, 1 packed, 2 dedicated, 3 external.
EXTERNAL_KIND = 3


def carry_external_descriptor(descriptor: object, base: str) -> object | None:
    """One scanned descriptor, mapped onto the shape a WRITE takes. None when it is not external.

    THE READ AND WRITE SHAPES ARE NOT SYMMETRIC, which is why this is a mapping and not a copy. A
    scan returns ``struct<kind, position, size, blob_id, blob_uri>``; a write takes a ``Blob``. Two
    details in between are silent if got wrong, and both were got wrong first:

    * ``blob_uri`` is RELATIVE to the base (``page-000.bin``, not a URI). Passing it through is
      refused at write — loudly, which is the good case.
    * ``size == 0`` means THE WHOLE OBJECT. Passing it back as a slice length asks for zero bytes and
      yields an empty read with no error at all — the silent case.
    """
    from lance.blob import Blob

    if not isinstance(descriptor, dict) or descriptor.get("kind") != EXTERNAL_KIND:
        return None
    relative = descriptor.get("blob_uri")
    if not relative:
        return None
    absolute = f"{base.rstrip('/')}/{relative}"
    size = descriptor.get("size") or 0
    if size:
        return Blob(uri=absolute, position=descriptor.get("position") or 0, size=size)
    return Blob.from_uri(absolute)


def read_aligned_table(
    ds: lance.LanceDataset,
    *,
    columns: list[str] | None = None,
    with_row_id: bool = False,
    limit: int | None = None,
) -> pa.Table:
    """One ROW-ALIGNED scan whose blob-v2 columns arrive as ``large_binary`` bytes, **nulls included**.

    ``blob_handling="all_binary"`` returns tabular and blob columns from the same scan, with a null payload as
    ``None`` on its own row, so a caller pairing payloads with the rest of the row needs no second read to keep them
    aligned, and the payload list can be handed straight back to :func:`lance.blob_array` (which accepts ``None``
    entries) to re-wrap a blob column for a 2.2 write.

    ``read_blobs`` and ``take_blobs`` keep a null row's slot too (measured on pylance 12.0.0: ``read_blobs`` over
    indices ``[0, 1, 2]`` with a null in slot 1 answers ``[(0, b'a'), (1, None), (2, b'c')]``, and ``take_blobs``
    answers ``[BlobFile, None, BlobFile]``). They read by row, and are the reads for single-row serving and for a
    known set of row ids.

    THIS IS A WHOLE-RESULT READ: ``to_table`` holds every selected payload at once. ``limit`` bounds it for a caller
    that needs a few rows; a caller that consumes every payload streams a bounded scanner instead ([[CP-051]]).
    """
    return ds.scanner(columns=columns, blob_handling="all_binary", with_row_id=with_row_id, limit=limit).to_table()
