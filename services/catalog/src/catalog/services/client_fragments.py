"""What a client-written data file must prove before ``CreateTableVersion`` finalizes a version that adds it ([[LH-211]], [[LH-471]]).

A stock writer stages a manifest whose fragments it described itself, and Lance's commit takes every field
of them on trust. Measured through a client-direct append on pylance 12.0.0, each of these COMMITTED: a ``file_size_bytes`` 100 bytes too large and a 64-byte garbage file
(every later read fails); an over-declared ``physical_rows`` (every read fails) and an under-declared one
(rows silently dropped); ``column_indices`` past the file's columns; a ``row_id_meta`` copied from a
committed fragment (duplicate stable row ids); a deletion file and an overlay (the overlay made the table
unopenable, reader flags 66); a file whose footer is 2.1 or 2.3 while it declares 2.2; the same file
listed twice (every field id in the table re-minted); ``fields`` permuted, unknown or carrying the ``-2``
tombstone (columns swapped or read as NULL); a path that leaves ``data/``; a fragment whose blob
sidecar is missing (every blob read fails); an external blob naming any object by absolute URI (``take_blobs``
then served its bytes) or a missing one; the same file in two fragments; and a file the table already held
(the row a delete removed came back). The INSERT lineage event then carried the false row count.

So the JSON is a claim and the data file is the authority. Its footer carries the row count, the column
count and the format version the writer actually wrote, and its schema names the columns
(lance_docs/lance/format/file/index.md, "Offsets & Footer" and "Reading Strategy": one or two reads from the end of
the file). The gates ``catalog.services.managed_versions`` runs on each data file a version adds:

* :func:`refuse_a_malformed_data_file`: a bare name under ``data/``, each field id once, sizes and versions that are integers.
* :func:`verify_against_footers`: each data file's footer agrees with what the fragment declares.
* :func:`verify_blob_sidecars`: every sidecar a blob descriptor points into exists and is long enough, and
  every external blob lies under one of the table's external bases and exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Protocol, cast

import pyarrow as pa
from lance.file import LanceFileReader
from lance_namespace import InvalidInputError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, ValidationError

from catalog.core import encoding
from service_kit.lakehouse import base_registry, blobs
from service_kit.lakehouse.base_refs import normalise, store_of
from service_kit.lakehouse.features import describe_foreign_data_file_versions
from service_kit.lakehouse.objectfs import StorageOptions


if TYPE_CHECKING:
    import lance
    from lance.fragment import DataFile, FragmentMetadata


#: A data file's path as ``write_fragments`` names it: one segment under ``data/``. Anything else (a
#: ``/``, a ``..``, a percent escape) is a path the existence check and Lance may resolve differently:
#: measured, a ``../`` path and a ``sub/`` path each passed the existence check and committed.
_BARE_DATA_FILE: Final = re.compile(r"[A-Za-z0-9_-]+\.lance")

#: The ``FragmentMetadata`` fields a fresh append leaves unset. Lance assigns row ids and both version
#: metas at commit; a deletion file or an overlay describes an edit to a committed fragment, which an
#: append has none of.
_UNSET_ON_APPEND: Final = ("row_id_meta", "created_at_version_meta", "last_updated_at_version_meta", "deletion_file")

#: The descriptor ``kind`` values whose bytes live in a ``.blob`` sidecar under ``data/<data-file-stem>/``.
#: Measured on pylance 12.0.0 with the ingest thresholds: a 1 KiB payload is kind 0 (inline in the data
#: file), 100 KiB and 200 KiB share one kind-1 sidecar (packed), and 5 MiB gets its own kind-2 sidecar
#: (dedicated).
_SIDECAR_KINDS: Final = (1, 2)

#: The descriptor ``kind`` of a blob whose bytes live outside the table. Measured on pylance 12.0.0: under
#: a registered non-root base (ingest's external base) ``write_fragments`` stores ``blob_id`` = that
#: base's id and a ``blob_uri`` relative to it; with ``allow_external_blob_outside_bases`` it stores
#: ``blob_id`` 0 and the absolute URI, and ``take_blobs`` on the table then returned the bytes of any
#: object that URI names, read with the reader's credentials.
_EXTERNAL_KIND: Final = 3

#: Inline, packed, dedicated and external. Managed kinds 4 and 5 need ``FLAG_MANAGED_BLOBS``, which the feature-flag rule
#: refuses, and a kind no rule here judges is an error (`lance_docs/lance/format/table/blob.md` § Null Descriptors).
_SERVED_KINDS: Final = (0, *_SIDECAR_KINDS, _EXTERNAL_KIND)

#: A ``blob_uri`` under a base: relative segments of the characters an object key under a base uses.
#: No scheme, no leading ``/``, no ``.`` or ``..`` segment, no percent escape or backslash.
_RELATIVE_BLOB_URI: Final = re.compile(r"(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9._~@=+,!-]+(?:/[A-Za-z0-9._~@=+,!-]+)*")

#: How pylance 12.0.0's ``LanceFileReader`` renders a blob-v2 column in a footer schema: the descriptor
#: as an empty ``struct<>`` carrying ``lance-encoding:blob``, then its five children as top-level fields
#: (measured). The descriptor is ONE physical column, so its children are not columns of their own.
_BLOB_DESCRIPTOR_CHILDREN: Final = ("kind", "position", "size", "blob_id", "blob_uri")

#: Lance's wording for a footer it cannot parse, measured on pylance 12.0.0: "Invalid user input: file
#: does not appear to be a Lance file" for garbage and a truncated tail, and "Version conflict error:
#: Attempt to use the Lance current-format reader to read v1 metadata" for a V1 file. A read that fails
#: any other way (the store unreachable: "HTTP error: error sending request") is the store's failure.
_UNREADABLE_FILE_MARKERS: Final = ("invalid user input", "version conflict error")


class _FooterMetadata(Protocol):
    """What :func:`verify_against_footers` reads from a footer.

    pylance 12.0.0's stub for ``LanceFileMetadata`` omits ``major_version`` and ``minor_version``, which
    the runtime object carries (measured).
    """

    @property
    def major_version(self) -> int: ...

    @property
    def minor_version(self) -> int: ...

    @property
    def num_rows(self) -> int: ...

    @property
    def schema(self) -> pa.Schema: ...

    @property
    def columns(self) -> Sequence[object]: ...


class _FooterVersion(BaseModel):
    """A footer's format version in the shape ``describe_foreign_data_file_versions`` judges."""

    model_config = ConfigDict(frozen=True)

    file_major_version: int
    file_minor_version: int


def _refuse(index: int, path: str | None, reason: str) -> InvalidInputError:
    where = f"fragment {index}" + (f", data file {path!r}" if path else "")
    return InvalidInputError(f"commit refused: {where}: {reason}. Nothing was committed")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _names(exc: BaseException, markers: tuple[str, ...]) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in markers)


def refuse_a_malformed_data_file(index: int, data_file: DataFile) -> None:
    path = data_file.path
    if not isinstance(path, str) or not _BARE_DATA_FILE.fullmatch(path):
        raise _refuse(index, None, f"data file path {path!r} is not a bare file name under the table's data/ directory")
    fields, columns = data_file.fields, data_file.column_indices
    if not isinstance(fields, list) or not fields or not all(_is_int(field) and field >= 0 for field in fields):
        raise _refuse(index, path, f"fields must be a non-empty list of field ids, got {fields!r}")
    if not isinstance(columns, list) or not all(_is_int(column) for column in columns):
        raise _refuse(index, path, f"column_indices must be a list of integers, got {columns!r}")
    if not (_is_int(data_file.file_major_version) and _is_int(data_file.file_minor_version)):
        raise _refuse(index, path, f"the file format version must be integers, got {data_file.file_major_version!r}.{data_file.file_minor_version!r}")
    size = data_file.file_size_bytes
    if size is not None and (not _is_int(size) or size <= 0):
        raise _refuse(index, path, f"file_size_bytes must be a positive integer or absent, got {size!r}")


def _declares_v2(data_file: DataFile) -> bool:
    return data_file.file_major_version >= 2 or (data_file.file_major_version, data_file.file_minor_version) == (0, 3)


def verify_against_footers(location: str, so: StorageOptions, fragments: Sequence[FragmentMetadata], table: lance.LanceDataset) -> None:
    """Refuse a fragment whose data files' footers contradict what it declares.

    ``table`` is the version the fragments' declared file versions were judged against. Each file's
    footer must report ``physical_rows`` rows, the table's format version, one column per declared field
    with ``column_indices`` naming them in order, and a schema whose columns are, in order, exactly the
    table's fields the declared ids name. The last is what catches a permuted ``fields``: Lance reads
    column ``column_indices[i]`` as field ``fields[i]`` and never consults the file's own schema.

    A commit whose declared versions mix V1 and V2 is left to Lance, which refuses it at commit (pinned
    by ``test_a_V1_V2_mix_is_a_400_not_a_503``). An all-V1 commit is refused here: ``LanceFileReader``
    cannot read a V1 footer ("Attempt to use the Lance current-format reader to read v1 metadata",
    measured), so nothing it declares can be verified.

    Raises:
        InvalidInputError: A footer contradicts its fragment, or the file is not a readable Lance file.
        ServiceUnavailableError: A footer could not be read for a reason other than the file's content.
    """
    table_is_v2 = table.data_storage_version.split(".", 1)[0] not in ("0", "1")
    declared = {_declares_v2(data_file) for fragment in fragments for data_file in fragment.files}
    if declared != {table_is_v2}:
        return
    if not table_is_v2:
        raise InvalidInputError(
            f"commit refused: the table's data_storage_version is {table.data_storage_version}, whose V1 data files Lance's current-format reader "
            "cannot read, so their row counts, columns and versions cannot be verified. Append through /insert"
        )
    paths = sorted({data_file.path for fragment in fragments for data_file in fragment.files})
    footers = dict(zip(paths, _read_footers(location, so, paths), strict=True))
    every_field = _format_version(table.data_storage_version) == (2, 0)
    for index, fragment in enumerate(fragments):
        for data_file in fragment.files:
            _verify_one(index, data_file, footers[data_file.path], fragment.physical_rows, table, every_field=every_field)


def _format_version(version: str) -> tuple[int, int]:
    major, _, minor = version.partition(".")
    return int(major), int(minor)


def _read_footers(location: str, so: StorageOptions, paths: Sequence[str]) -> list[_FooterMetadata | Exception]:
    """Each file's footer, read concurrently: one or two ranged reads from the end of each file."""
    root = location.rstrip("/") + "/data/"
    options = dict(so) if so else None

    def _read(path: str) -> _FooterMetadata | Exception:
        try:
            return cast(_FooterMetadata, LanceFileReader(root + path, storage_options=options).metadata())
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=min(8, len(paths))) as pool:
        return list(pool.map(_read, paths))


def _verify_one(
    index: int, data_file: DataFile, footer: _FooterMetadata | Exception, physical_rows: int, table: lance.LanceDataset, *, every_field: bool
) -> None:
    path = data_file.path
    if isinstance(footer, Exception):
        if _names(footer, _UNREADABLE_FILE_MARKERS):
            raise _refuse(index, path, f"the file is not a readable Lance file: {footer}") from footer
        raise ServiceUnavailableError(f"cannot read the footer of data file {path!r} to verify the fragment, so nothing was committed: {footer}") from footer
    if footer.num_rows != physical_rows:
        raise _refuse(index, path, f"its footer holds {footer.num_rows} rows and the fragment declares physical_rows={physical_rows}")
    written = _FooterVersion(file_major_version=footer.major_version, file_minor_version=footer.minor_version)
    if describe_foreign_data_file_versions(table.data_storage_version, [written]) is not None:
        declared = f"{data_file.file_major_version}.{data_file.file_minor_version}"
        raise _refuse(
            index,
            path,
            f"its footer is file format {footer.major_version}.{footer.minor_version} while it declares {declared} and the table's "
            f"data_storage_version is {table.data_storage_version}",
        )
    columns = len(footer.columns)
    if data_file.column_indices != list(range(columns)) or len(data_file.fields) != columns:
        raise _refuse(
            index,
            path,
            f"its footer has {columns} columns, so it declares {columns} fields with column_indices {list(range(columns))}; "
            f"it declares fields {data_file.fields} with column_indices {data_file.column_indices}",
        )
    expected = _fields_the_footer_names(index, path, footer.schema, table, every_field=every_field)
    if data_file.fields != expected:
        raise _refuse(index, path, f"its columns are the table's field ids {expected} in that order, and it declares {data_file.fields}")


def _footer_columns(schema: pa.Schema) -> list[pa.Field]:
    """The footer schema's top-level columns, each blob descriptor's flattened children folded back into it.

    The children follow the top-level column holding the descriptor, one run of five per descriptor in it, at any depth
    (measured on pylance 13.0.0 with blobs beneath a Struct, a List and both).
    """
    fields = list(schema)
    columns: list[pa.Field] = []
    position = 0
    while position < len(fields):
        field = fields[position]
        columns.append(field)
        position += 1
        flattened = _BLOB_DESCRIPTOR_CHILDREN * _descriptors_in(field)
        if flattened and tuple(child.name for child in fields[position : position + len(flattened)]) == flattened:
            position += len(flattened)
    return columns


def _is_blob_descriptor(field: pa.Field) -> bool:
    return (field.metadata or {}).get(b"lance-encoding:blob") == b"true" and pa.types.is_struct(field.type) and field.type.num_fields == 0


def _descriptors_in(field: pa.Field) -> int:
    """How many blob descriptors a footer column holds beneath its Structs, Lists and LargeLists."""
    return 1 if _is_blob_descriptor(field) else sum(_descriptors_in(child) for child in blobs.nested_fields(field))


def _footer_matches(column: pa.Field, declared: pa.Field) -> bool:
    """Whether a footer column is the table's field, each blob-v2 field beneath it read as the descriptor the file stores.

    A data file stores a blob field as its descriptor, wherever the field lies (`lance_docs/lance/format/table/blob.md`
    § Data File Schema), so a subtree holding one is compared by shape and every other type exactly.
    """
    if blobs.is_blob_field(declared):
        return _is_blob_descriptor(column)
    if not blobs.holds_blob(declared):
        return column.type.equals(declared.type)
    found, wanted = column.type, declared.type
    if pa.types.is_struct(wanted):
        return (
            pa.types.is_struct(found)
            and found.num_fields == wanted.num_fields
            and all(found.field(i).name == wanted.field(i).name and _footer_matches(found.field(i), wanted.field(i)) for i in range(wanted.num_fields))
        )
    if pa.types.is_list(wanted):
        return pa.types.is_list(found) and _footer_matches(found.value_field, wanted.value_field)
    return pa.types.is_large_list(wanted) and pa.types.is_large_list(found) and _footer_matches(found.value_field, wanted.value_field)


def _fields_the_footer_names(index: int, path: str, schema: pa.Schema, table: lance.LanceDataset, *, every_field: bool) -> list[int]:
    """The field ids, in column order, of the table fields this footer's columns are.

    Which ids a data file lists is Lance's per-version rule, measured on pylance 12.0.0 with struct,
    list, fixed-size-list, nested-struct and map columns: a 2.0 file lists every field depth-first,
    parents included; 2.1 and 2.2 list only leaves, and a blob-v2 column as the one id of its
    descriptor. A column the table lacks, or whose type differs from the table's, matches no field.
    """
    by_name = {field.name(): field for field in table.lance_schema.fields()}
    expected: list[int] = []
    for column in _footer_columns(schema):
        field = by_name.get(column.name)
        if field is None:
            raise _refuse(index, path, f"its footer holds a column {column.name!r} the table does not have")
        declared = table.schema.field(column.name)
        if not _footer_matches(column, declared):
            if blobs.is_blob_field(declared):
                raise _refuse(index, path, f"the table's blob column {column.name!r} is not a blob descriptor in the file")
            if blobs.holds_blob(declared):
                raise _refuse(
                    index,
                    path,
                    f"its column {column.name!r} is {column.type}, and the table's {declared.type} is stored with each blob field as an empty "
                    "struct carrying lance-encoding:blob and every other field as the table types it",
                )
            raise _refuse(index, path, f"its column {column.name!r} is {column.type} and the table's is {declared.type}")
        expected.extend(_column_ids(field, declared, every_field=every_field))
    return expected


class _LanceField(Protocol):
    def id(self) -> int: ...

    def name(self) -> str: ...

    def children(self) -> Sequence[_LanceField]: ...


def _is_packed(declared: pa.Field, *, every_field: bool) -> bool:
    """A struct Lance packs is one column: "Only leaf fields and packed structs have column indices" (`lance_docs/lance/format/table/index.md`
    § Data Files). Measured on pylance 13.0.0, a 2.0 file packs a struct carrying any ``packed`` value and a 2.1 or 2.2
    file only one whose value Lance reads as true."""
    return encoding.packs_at_2_0(declared) if every_field else encoding.packs(declared)


def _column_ids(field: _LanceField, declared: pa.Field, *, every_field: bool) -> list[int]:
    """A blob-v2 field is one column, its descriptor's, wherever it lies (`lance_docs/lance/format/table/blob.md` § Data File Schema)."""
    children = field.children()
    if not children or blobs.is_blob_field(declared) or _is_packed(declared, every_field=every_field):
        return [field.id()]
    declared_children = (declared.type.field(i) for i in range(declared.type.num_fields))
    nested = [column for child, arrow in zip(children, declared_children, strict=True) for column in _column_ids(child, arrow, every_field=every_field)]
    return [field.id(), *nested] if every_field else nested


def blob_columns(table: lance.LanceDataset) -> list[str]:
    """The table's columns that are or hold a blob-v2 field: when there are any, the commit door verifies every descriptor in them."""
    return [field.name for field in table.schema if blobs.holds_blob(field)]


class _Descriptor(BaseModel):
    """One non-null blob descriptor a new fragment stores, where it lies, and the field whose data file holds it."""

    model_config = ConfigDict(frozen=True)

    where: str
    fragment_id: int
    field_id: int
    kind: int
    position: int
    size: int
    blob_id: int
    blob_uri: str


def verify_blob_sidecars(
    detached: lance.LanceDataset,
    fragments: Sequence[FragmentMetadata],
    columns: Sequence[str],
    *,
    external_bases: Sequence[base_registry.RecordedBase],
    object_sizes: Callable[[Sequence[str]], list[int | None]],
    sidecar_sizes: Callable[[Sequence[str]], list[int | None]],
) -> None:
    """Refuse fragments whose blob descriptors point at bytes that are missing, short, or not the table's.

    ``detached`` is a detached commit of exactly these fragments: Lance reads blob descriptors only through a dataset,
    and ``LanceFileReader`` cannot project a blob-v2 column on pylance 12.0.0 (it reads ``struct<>`` with no rows, or
    refuses the projection, measured). Every descriptor is judged, wherever its field lies beneath a Struct, List or
    LargeList (`lance_docs/lance/format/table/blob.md` § Table Schema).

    A sidecar is ``<data file stem>/<encoded blob_id>.blob`` beside the data file that holds the descriptor's field
    (§ Sidecar Resolution; a new fragment's files are all under the ref's ``data/``, any other was refused before this
    runs), and ``sidecar_sizes`` answers each, by that path under ``data/``, in one batch. Each must hold the furthest
    byte any descriptor names in it. Its size is read rather than its bytes through ``take_blobs``, which refuses a
    blob beneath a List (measured on pylance 13.0.0).

    An external blob must name, by ``blob_id``, a base the table registered that is one of ``external_bases``, with a
    ``blob_uri`` relative to it, and its object must exist and hold the slice it names; ``object_sizes`` answers every
    such object in one batch (``None`` for one that is absent). ``external_bases`` is the catalog's record of the
    external blob bases the create authorized for this table ([[LH-209]]), never the manifest's list or the configured
    allowlist: a holder of the table's write credential can add any base to the manifest, and a base inside an
    allowlisted prefix can still cover another tenant's objects.

    Raises:
        InvalidInputError: A sidecar is missing or shorter than a descriptor says, or an external blob is outside the
            table's external bases or missing.
        ServiceUnavailableError: The descriptors or a sidecar's size could not be read for a reason other than their content.
    """
    written = {data_file.path for fragment in fragments for data_file in fragment.files}
    added = [fragment for fragment in detached.get_fragments() if {data_file.path for data_file in fragment.metadata.files} <= written]
    try:
        scanned = detached.scanner(columns=list(columns), fragments=added, with_row_address=True).to_table()
    except OSError as exc:
        if _names(exc, _UNREADABLE_FILE_MARKERS):
            raise InvalidInputError(f"commit refused: the appended fragments' blob descriptors are unreadable: {exc}. Nothing was committed") from exc
        raise ServiceUnavailableError(
            f"cannot read the appended fragments' blob descriptors to verify their sidecars, so nothing was committed: {exc}"
        ) from exc
    descriptors = list(_descriptors(scanned, detached, columns))
    _verify_external_blobs(descriptors, _external_bases(detached, external_bases), object_sizes)
    _verify_sidecars(descriptors, {fragment.fragment_id: fragment.metadata.files for fragment in added}, sidecar_sizes)


def _descriptors(scanned: pa.Table, table: lance.LanceDataset, columns: Sequence[str]) -> Iterator[_Descriptor]:
    ids = dict(_field_ids(table.lance_schema.fields(), ()))
    for column in columns:
        declared = table.schema.field(column)
        for value, address in zip(scanned.column(column).to_pylist(), scanned.column("_rowaddr").to_pylist(), strict=True):
            fragment, row = int(address) >> 32, int(address) & 0xFFFFFFFF
            for path, names, blob in blobs.blob_descriptors(value, declared):
                where = f"fragment {fragment} row {row} of blob column {path!r}"
                if blob.get("kind") not in _SERVED_KINDS:
                    raise InvalidInputError(
                        f"commit refused: {where} is a blob of kind {blob.get('kind')}, and this catalog judges kinds {list(_SERVED_KINDS)}. Nothing was committed"
                    )
                try:
                    yield _Descriptor.model_validate({**blob, "where": where, "fragment_id": fragment, "field_id": ids[names]})
                except ValidationError as exc:
                    raise InvalidInputError(
                        f"commit refused: {where} is a blob of kind {blob.get('kind')} without a value its kind needs: {blob}. Nothing was committed"
                    ) from exc


def _field_ids(fields: Sequence[_LanceField], parent: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], int]]:
    """``(field names from the column down, field id)`` for every field of a Lance schema."""
    for field in fields:
        names = (*parent, field.name())
        yield names, field.id()
        yield from _field_ids(field.children(), names)


def _external_bases(table: lance.LanceDataset, recorded: Sequence[base_registry.RecordedBase]) -> dict[int, str]:
    """``{base id: base URI}`` of the table's registered non-root bases its record holds as external blob bases, by path and store.

    ``base_paths()`` is the library's own answer for the manifest's bases, as
    ``service_kit.lakehouse.features.manifest_base_path_refs`` reads it.
    """
    allowed = {(entry.path, entry.store) for entry in recorded if entry.role is base_registry.BaseRole.EXTERNAL_BLOB and not entry.is_dataset_root}
    declared = table.base_paths()
    return {base_id: base.path for base_id, base in declared.items() if not base.is_dataset_root and (normalise(base.path), store_of(base.path)) in allowed}


def _verify_external_blobs(descriptors: Sequence[_Descriptor], bases: dict[int, str], object_sizes: Callable[[Sequence[str]], list[int | None]]) -> None:
    pointers: list[tuple[str, _Descriptor]] = []
    for blob in descriptors:
        if blob.kind != _EXTERNAL_KIND:
            continue
        if not bases:
            raise InvalidInputError(
                f"commit refused: {blob.where} points at {blob.blob_uri!r} as an external blob, and the table has no external blob base. Nothing was committed"
            )
        base = bases.get(blob.blob_id)
        if base is None or not _RELATIVE_BLOB_URI.fullmatch(blob.blob_uri):
            raise InvalidInputError(
                f"commit refused: {blob.where} points at {blob.blob_uri!r} through base id {blob.blob_id}; an external blob is a path relative to one of the "
                f"table's external blob bases {sorted(bases.values())}. Nothing was committed"
            )
        pointers.append((f"{base.rstrip('/')}/{blob.blob_uri}", blob))
    if not pointers:
        return
    for (uri, blob), stored in zip(pointers, object_sizes([uri for uri, _ in pointers]), strict=True):
        if stored is None or blob.position + blob.size > stored:
            found = "does not exist" if stored is None else f"holds {stored} bytes"
            raise InvalidInputError(
                f"commit refused: {blob.where} names bytes {blob.position}..{blob.position + blob.size} of {uri}, which {found}. Nothing was committed"
            )


def _verify_sidecars(
    descriptors: Sequence[_Descriptor], files: dict[int, Sequence[DataFile]], sidecar_sizes: Callable[[Sequence[str]], list[int | None]]
) -> None:
    """Every sidecar a descriptor names exists and reaches the furthest byte any descriptor names in it."""
    furthest: dict[str, tuple[int, _Descriptor]] = {}
    for blob in descriptors:
        if blob.kind not in _SIDECAR_KINDS or not blob.size:
            continue
        owner = next((data_file for data_file in files[blob.fragment_id] if blob.field_id in data_file.fields), None)
        if owner is None:
            raise InvalidInputError(f"commit refused: {blob.where} is stored by no data file of its fragment. Nothing was committed")
        sidecar, end = f"{owner.path.removesuffix('.lance')}/{format(blob.blob_id, '032b')[::-1]}.blob", blob.position + blob.size
        if sidecar not in furthest or end > furthest[sidecar][0]:
            furthest[sidecar] = (end, blob)
    if not furthest:
        return
    try:
        stored = sidecar_sizes(list(furthest))
    except OSError as exc:
        raise ServiceUnavailableError(f"cannot read the size of the appended fragments' blob sidecars to verify them, so nothing was committed: {exc}") from exc
    for (sidecar, (end, blob)), size in zip(furthest.items(), stored, strict=True):
        if size is None or size < end:
            found = "is missing" if size is None else f"holds {size} bytes"
            raise InvalidInputError(
                f"commit refused: {blob.where} points into a blob sidecar that is missing or shorter than it says: it names bytes up to {end} of "
                f"data/{sidecar}, which {found}. Nothing was committed"
            )
