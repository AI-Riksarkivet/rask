"""What a client-written fragment must prove before ``/commit`` folds it into a version ([[LH-211]]).

The client-direct append receives ``FragmentMetadata`` that a vended writer serialized, and Lance's
commit takes every field of it on trust. Measured through ``commit_appended_fragments`` on pylance
12.0.0, each of these COMMITTED: a ``file_size_bytes`` 100 bytes too large and a 64-byte garbage file
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
(lance_docs/file_format.md, "Offsets & Footer" and "Reading Strategy": one or two reads from the end of
the file). Three gates, in the order the commit door runs them:

* :func:`refuse_forged_metadata`: what the JSON alone shows. A fresh append carries no row ids, version
  metas, deletion file or overlay (``write_fragments`` leaves all of them unset, with or without
  ``enable_stable_row_ids``, measured), names its files bare under ``data/``, lists each field id once, and names each file once.
* :func:`refuse_files_the_table_holds`: no file the table already references.
* :func:`verify_against_footers`: each data file's footer agrees with what the fragment declares.
* :func:`verify_blob_sidecars`: every sidecar a blob descriptor points into exists and is long enough, and
  every external blob lies under one of the table's external bases and exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Final, Protocol, cast

import pyarrow as pa
from lance.file import LanceFileReader
from lance_namespace import InvalidInputError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

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

#: Lance's wording for a sidecar read that fails because of the sidecar, measured on pylance 12.0.0:
#: "Not found: <path>" when it is missing (S3 and a local disk), S3's 416 "Range Not Satisfiable" /
#: ``InvalidRange`` and the local "failed to fill whole buffer" when it is short. Every one of them is
#: wrapped in "Failed to read blob source", the store being unreachable included, so that is no marker.
_SIDECAR_FAULT_MARKERS: Final = ("not found:", "range not satisfiable", "invalidrange", "failed to fill whole buffer")


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


def refuse_forged_metadata(fragments: Sequence[FragmentMetadata]) -> None:
    """Refuse what the fragment JSON alone shows a fresh append cannot carry.

    Raises:
        InvalidInputError: A fragment carries row ids, version metas, a deletion file or an overlay, has
            no data file or a malformed one, names a path outside ``data/``, or lists a field id twice
            or a negative one (``-2`` is Lance's tombstone).
    """
    named: dict[str, int] = {}
    for index, fragment in enumerate(fragments):
        carried = [name for name in _UNSET_ON_APPEND if getattr(fragment, name) is not None]
        if fragment.overlays:
            carried.append("overlays")
        if carried:
            raise _refuse(index, None, f"an appended fragment carries none of {', '.join(carried)}; Lance assigns them at commit or they describe an edit")
        if not _is_int(fragment.physical_rows) or fragment.physical_rows < 0:
            raise _refuse(index, None, f"physical_rows must be a non-negative integer, got {fragment.physical_rows!r}")
        if not fragment.files:
            raise _refuse(index, None, "a fragment with no data file reads every column as NULL")
        seen: set[int] = set()
        for data_file in fragment.files:
            _refuse_a_malformed_data_file(index, data_file)
            repeated = seen.intersection(data_file.fields)
            if repeated or len(set(data_file.fields)) != len(data_file.fields):
                raise _refuse(index, data_file.path, f"field ids {sorted(repeated) or data_file.fields} are listed twice; each field lives in one data file")
            seen.update(data_file.fields)
        for path in {data_file.path for data_file in fragment.files}:
            if (first := named.setdefault(path, index)) != index:
                raise _refuse(index, path, f"fragment {first} names the same file; a file appended twice reads its rows twice")


def _refuse_a_malformed_data_file(index: int, data_file: DataFile) -> None:
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


def refuse_files_the_table_holds(fragments: Sequence[FragmentMetadata], table: lance.LanceDataset) -> None:
    """Refuse a data file the table already references.

    Re-appending a committed fragment's file reads its rows again, deleted ones included: measured on
    pylance 12.0.0, after ``delete("id = 0")`` the re-appended file brought the deleted row back, so a
    writer could undo any delete, an erasure among them. ``table`` is the version the append is made
    against; its manifest names every file it holds, so this reads no object.

    Raises:
        InvalidInputError: A fragment names a file the table already holds.
    """
    held = {data_file.path for fragment in table.get_fragments() for data_file in fragment.metadata.files}
    for index, fragment in enumerate(fragments):
        for data_file in fragment.files:
            if data_file.path in held:
                raise _refuse(index, data_file.path, "the table already holds this file; appending it again reads its rows twice and restores deleted ones")


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
    """The footer schema's top-level columns, each blob descriptor's flattened children folded back into it."""
    fields = list(schema)
    columns: list[pa.Field] = []
    position = 0
    while position < len(fields):
        field = fields[position]
        columns.append(field)
        position += 1
        following = tuple(child.name for child in fields[position : position + len(_BLOB_DESCRIPTOR_CHILDREN)])
        if _is_blob_descriptor(field) and following == _BLOB_DESCRIPTOR_CHILDREN:
            position += len(_BLOB_DESCRIPTOR_CHILDREN)
    return columns


def _is_blob_descriptor(field: pa.Field) -> bool:
    return (field.metadata or {}).get(b"lance-encoding:blob") == b"true" and pa.types.is_struct(field.type) and field.type.num_fields == 0


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
        if blobs.is_blob_field(declared):
            if not _is_blob_descriptor(column):
                raise _refuse(index, path, f"the table's blob column {column.name!r} is not a blob descriptor in the file")
            expected.append(field.id())
            continue
        if not column.type.equals(declared.type):
            raise _refuse(index, path, f"its column {column.name!r} is {column.type} and the table's is {declared.type}")
        expected.extend(_column_ids(field, every_field=every_field))
    return expected


class _LanceField(Protocol):
    def id(self) -> int: ...

    def children(self) -> Sequence[_LanceField]: ...


def _column_ids(field: _LanceField, *, every_field: bool) -> list[int]:
    children = field.children()
    if not children:
        return [field.id()]
    nested = [column for child in children for column in _column_ids(child, every_field=every_field)]
    return [field.id(), *nested] if every_field else nested


def blob_columns(table: lance.LanceDataset) -> list[str]:
    """The table's blob-v2 columns: when there are any, the commit door verifies sidecars on a detached commit."""
    return blobs.blob_field_names(table.schema)


def verify_blob_sidecars(
    detached: lance.LanceDataset,
    fragments: Sequence[FragmentMetadata],
    columns: Sequence[str],
    *,
    external_bases: Sequence[base_registry.RecordedBase],
    object_sizes: Callable[[Sequence[str]], list[int | None]],
) -> None:
    """Refuse fragments whose blob descriptors point at bytes that are missing, short, or not the table's.

    ``detached`` is a detached commit of exactly these fragments: Lance reads blob descriptors only
    through a dataset, and ``LanceFileReader`` cannot project a blob-v2 column on pylance 12.0.0 (it
    reads ``struct<>`` with no rows, or refuses the projection, measured). For each sidecar, the blob
    that reaches furthest into it is read at its last byte: one ranged read per sidecar, which proves the
    file exists and holds every blob packed into it.

    An external blob must name, by ``blob_id``, a base the table registered that is one of
    ``external_bases``, with a ``blob_uri`` relative to it, and its object must exist and hold the slice
    it names; ``object_sizes`` answers every such object in one batch (``None`` for one that is absent).
    ``external_bases`` is the catalog's record of the external blob bases the create authorized for this
    table ([[LH-209]]), never the manifest's list or the configured allowlist: a holder of the table's
    write credential can add any base to the manifest, and a base inside an allowlisted prefix can still
    cover another tenant's objects.

    Raises:
        InvalidInputError: A sidecar is missing or shorter than a descriptor says, or an external blob is
            outside the table's external bases or missing.
        ServiceUnavailableError: A sidecar could not be read for a reason other than its absence or length.
    """
    written = {data_file.path for fragment in fragments for data_file in fragment.files}
    added = [fragment for fragment in detached.get_fragments() if {data_file.path for data_file in fragment.metadata.files} <= written]
    try:
        descriptors = detached.scanner(columns=list(columns), fragments=added, with_row_address=True).to_table()
    except OSError as exc:
        if _names(exc, _UNREADABLE_FILE_MARKERS):
            raise InvalidInputError(f"commit refused: the appended fragments' blob descriptors are unreadable: {exc}. Nothing was committed") from exc
        raise ServiceUnavailableError(
            f"cannot read the appended fragments' blob descriptors to verify their sidecars, so nothing was committed: {exc}"
        ) from exc
    bases = _external_bases(detached, external_bases)
    for column in columns:
        _verify_external_blobs(descriptors, column, bases, object_sizes)
        furthest = _furthest_blob_per_sidecar(descriptors, column)
        if not furthest:
            continue
        handles = detached.take_blobs(column, addresses=[address for address, _ in furthest])
        for (address, size), handle in zip(furthest, handles, strict=True):
            _read_last_byte(column, address, size, handle)


class _BasePath(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def is_dataset_root(self) -> bool: ...


class _BasePaths(Protocol):
    def base_paths(self) -> dict[int, _BasePath]: ...


def _external_bases(table: lance.LanceDataset, recorded: Sequence[base_registry.RecordedBase]) -> dict[int, str]:
    """``{base id: base URI}`` of the table's registered non-root bases its record holds as external blob bases, by path and store.

    ``_ds.base_paths()`` is the library's own answer for the manifest's bases, as
    ``service_kit.lakehouse.features.manifest_base_path_refs`` reads it.
    """
    allowed = {(entry.path, entry.store) for entry in recorded if entry.role is base_registry.BaseRole.EXTERNAL_BLOB and not entry.is_dataset_root}
    declared = cast(_BasePaths, getattr(table, "_ds")).base_paths()  # noqa: B009 — pylance's private handle, typed by the Protocol
    return {base_id: base.path for base_id, base in declared.items() if not base.is_dataset_root and (normalise(base.path), store_of(base.path)) in allowed}


def _verify_external_blobs(descriptors: pa.Table, column: str, bases: dict[int, str], object_sizes: Callable[[Sequence[str]], list[int | None]]) -> None:
    pointers: list[tuple[str, int, int, str]] = []
    for row in descriptors.select([column, "_rowaddr"]).to_pylist():
        blob = row[column]
        if blob is None or blob["kind"] != _EXTERNAL_KIND:
            continue
        address = int(row["_rowaddr"])
        where = f"fragment {address >> 32} row {address & 0xFFFFFFFF} of blob column {column!r}"
        if not bases:
            raise InvalidInputError(f"commit refused: {where} is an external blob and the table has no external blob base. Nothing was committed")
        base, relative = bases.get(int(blob["blob_id"])), str(blob["blob_uri"])
        if base is None or not _RELATIVE_BLOB_URI.fullmatch(relative):
            raise InvalidInputError(
                f"commit refused: {where} points at {relative!r} through base id {blob['blob_id']}; an external blob is a path relative to one of the "
                f"table's external blob bases {sorted(bases.values())}. Nothing was committed"
            )
        pointers.append((f"{base.rstrip('/')}/{relative}", int(blob["position"]), int(blob["size"]), where))
    if not pointers:
        return
    for (uri, position, size, where), stored in zip(pointers, object_sizes([uri for uri, *_ in pointers]), strict=True):
        if stored is None or position + size > stored:
            found = "does not exist" if stored is None else f"holds {stored} bytes"
            raise InvalidInputError(f"commit refused: {where} names bytes {position}..{position + size} of {uri}, which {found}. Nothing was committed")


def _furthest_blob_per_sidecar(descriptors: pa.Table, column: str) -> list[tuple[int, int]]:
    """``(row address, size)`` of the blob ending furthest into each sidecar this column's rows use.

    A sidecar is ``(fragment, blob_id)``: it lives under its data file's stem, and an appended fragment
    has one data file.
    """
    furthest: dict[tuple[int, int], tuple[int, int, int]] = {}
    for row in descriptors.select([column, "_rowaddr"]).to_pylist():
        blob = row[column]
        if blob is None or blob["kind"] not in _SIDECAR_KINDS or not blob["size"]:
            continue
        address, size = int(row["_rowaddr"]), int(blob["size"])
        sidecar, end = (address >> 32, int(blob["blob_id"])), int(blob["position"]) + size
        if sidecar not in furthest or end > furthest[sidecar][0]:
            furthest[sidecar] = (end, address, size)
    return [(address, size) for _, address, size in furthest.values()]


def _read_last_byte(column: str, address: int, size: int, handle: object) -> None:
    fragment, row = address >> 32, address & 0xFFFFFFFF
    where = f"fragment {fragment} row {row} of blob column {column!r}"
    if handle is None:
        raise InvalidInputError(f"commit refused: {where} names a {size}-byte blob that Lance reads as null. Nothing was committed")
    blob = cast(_BlobFile, handle)
    try:
        blob.seek(size - 1)
        tail = blob.read(1)
    except (OSError, ValueError) as exc:
        if _names(exc, _SIDECAR_FAULT_MARKERS):
            raise InvalidInputError(
                f"commit refused: {where} points into a blob sidecar that is missing or shorter than it says: {exc}. Nothing was committed"
            ) from exc
        raise ServiceUnavailableError(f"cannot read the blob sidecar behind {where} to verify it, so nothing was committed: {exc}") from exc
    if len(tail) != 1:
        raise InvalidInputError(f"commit refused: {where} names {size} bytes and its sidecar ends before them. Nothing was committed")


class _BlobFile(Protocol):
    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def read(self, size: int = -1, /) -> bytes: ...
