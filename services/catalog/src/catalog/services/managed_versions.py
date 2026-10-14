"""The catalog's ``CreateTableVersion``: judge a client-staged manifest, then finalize it ([[LH-471]], ADR 0157).

Under ``managed_versioning`` a stock writer puts its data, transaction and deletion files and a staged
``_versions/<n>.manifest-<uuid>``, and asks the catalog to finalize it. The catalog judges the version that manifest
describes before anything is final, so a refusal leaves the table at its version.

pylance opens no staged manifest, but it opens a DETACHED one: ``_versions/d<id>.manifest`` with the top bit of ``id``
set is read by ``lance.dataset(location, version=id)`` and never becomes the latest version. The staged manifest is
copied there with one change, its ``version`` field set to ``id``: Lance refuses a manifest whose own version is not
the one asked for (measured on pylance 13.0.0, "Version not found" for an unchanged copy), and a create has no
committed version to ``checkout_version`` from. The copy is judged through pylance and deleted whatever the verdict. The finalize is
the backend's own put-if-not-exists (``create_table_version`` of the ``dir`` namespace), so two writers racing for one
version number meet the store's arbitration, not the catalog's.
"""

from __future__ import annotations

import logging
import secrets
import struct
from collections.abc import Sequence
from functools import partial

import lance
import pyarrow as pa
import pyarrow.compute as pc
from lance.fragment import DataFile
from lance_namespace import CreateTableVersionRequest, CreateTableVersionResponse, InvalidInputError, LanceNamespace, UnsupportedOperationError
from pydantic import BaseModel, InstanceOf

from catalog.core import lineage_emit, provenance_guard
from catalog.core.base_judge import BaseJudge, require_sanctioned_bases
from catalog.services import client_fragments, dataplane, maintenance, native, primary_keys
from service_kit.lakehouse import base_registry, features
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: A detached version id: the top bit set, as Lance's own detached commits are (``d<id>.manifest``).
_DETACHED_BIT = 1 << 63

#: A manifest file ends ``<u64 offset of the Manifest message> <u16 major> <u16 minor> LANC``, and the message at that
#: offset is ``<u32 length> <protobuf>`` (pylance 13.0.0 manifests, read back in the test of this module).
_MAGIC = b"LANC"
_FOOTER = struct.Struct("<QHH4s")
_LENGTH = struct.Struct("<I")


#: ``uint64 version = 3`` of ``message Manifest`` (Lance ``protos/table.proto`` at v13.0.0): a varint, wire type 0.
_VERSION_FIELD = 3


def _read_varint(buffer: bytes, at: int) -> tuple[int, int]:
    """The varint at ``at`` and the offset after it."""
    value, shift = 0, 0
    while True:
        if at >= len(buffer) or shift > 63:
            raise InvalidInputError("the staged manifest's Manifest message is truncated")
        byte = buffer[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if not byte & 0x80:
            return value, at
        shift += 7


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if not value:
            out.append(low)
            return bytes(out)
        out.append(low | 0x80)


def _with_version_field(message: bytes, version: int) -> bytes:
    """``message`` with its top-level field 3 (a varint) replaced by ``version``, every other field kept byte for byte."""
    out, at, written = bytearray(), 0, False
    while at < len(message):
        start = at
        key, at = _read_varint(message, at)
        number, wire = key >> 3, key & 0x7
        if wire == 0:
            _, at = _read_varint(message, at)
        elif wire == 1:
            at += 8
        elif wire == 2:
            length, at = _read_varint(message, at)
            at += length
        elif wire == 5:
            at += 4
        else:
            raise InvalidInputError(f"the staged manifest's Manifest message has an unsupported wire type {wire}")
        if at > len(message):
            raise InvalidInputError("the staged manifest's Manifest message is truncated")
        if number == _VERSION_FIELD and wire == 0:
            out += _varint(key) + _varint(version)
            written = True
        else:
            out += message[start:at]
    if not written:
        out += _varint(_VERSION_FIELD << 3) + _varint(version)
    return bytes(out)


def with_manifest_version(manifest: bytes, version: int) -> bytes:
    """``manifest`` with its ``version`` field set to ``version`` and every other byte's meaning unchanged.

    The sections before the Manifest message (index and transaction sections, addressed by absolute offsets) are kept
    byte for byte; only the message and the footer's offset to it are written again.

    Raises:
        InvalidInputError: ``manifest`` does not end in a Lance manifest footer.
    """
    if len(manifest) < _FOOTER.size or manifest[-4:] != _MAGIC:
        raise InvalidInputError("the staged manifest is not a Lance manifest (no LANC footer)")
    offset, major, minor, _ = _FOOTER.unpack(manifest[-_FOOTER.size :])
    if offset + _LENGTH.size > len(manifest) - _FOOTER.size:
        raise InvalidInputError("the staged manifest's footer points outside the file")
    (length,) = _LENGTH.unpack_from(manifest, offset)
    body = _with_version_field(manifest[offset + _LENGTH.size : offset + _LENGTH.size + length], version)
    return manifest[:offset] + _LENGTH.pack(len(body)) + body + _FOOTER.pack(offset, major, minor, _MAGIC)


class StagedVersion(BaseModel):
    """A staged manifest planted as a detached version, and what its transaction says."""

    location: str
    detached_id: int
    #: The catalog-written copy the backend finalizes from: the writer can put over its own staged key until the
    #: finalize, and the write tier's explicit Deny on ``_versions/*.manifest`` keeps it off this one.
    finalize_id: int
    #: The writer's staged manifest, which the backend no longer consumes: deleted once the version lands.
    staged_uri: str
    version: int
    operation: str

    @property
    def creates(self) -> bool:
        """The table's first version: there is no committed version for it to follow."""
        return self.version == 1


def _object_store_path(location: str) -> str:
    """``location`` as Lance's object store names it: no scheme, no bucket, no leading slash."""
    if location.startswith("s3://"):
        return location.removeprefix("s3://").partition("/")[2].strip("/")
    return location.removeprefix("file://").strip("/")


def _staged_uri(location: str, manifest_path: str) -> str:
    """The staged manifest's URI, refusing any path but one staged name directly under this table's ``_versions/``.

    pylance sends ``manifest_path`` as an object-store path (measured on 13.0.0: the table's path with no scheme and
    no leading slash, then ``_versions/<n>.manifest-<uuid>``), so it is compared in that form.
    """
    root = location.rstrip("/")
    prefix = f"{_object_store_path(root)}/_versions/"
    name = manifest_path.removeprefix(prefix) if manifest_path.startswith(prefix) else ""
    if not name or "/" in name or ".manifest-" not in name or name.endswith(".manifest"):
        raise InvalidInputError(f"manifest_path {manifest_path!r} is not a staged manifest under this table's _versions/")
    return f"{root}/_versions/{name}"


def _latest_version(location: str, so: StorageOptions) -> int:
    """The table's latest committed version, 0 for a declared table with none."""
    try:
        return int(lance.dataset(location, storage_options=dict(so) if so else None).version)
    except (OSError, ValueError):
        return 0


def _write(location: str, so: StorageOptions, name: str, body: bytes) -> None:
    fs, base = dataplane.dataset_fs(location, so)
    with fs.open_output_stream(f"{base.rstrip('/')}/_versions/{name}") as sink:
        sink.write(body)


def stage(location: str, so: StorageOptions, request: CreateTableVersionRequest) -> StagedVersion:
    """Read the staged manifest ``request`` names once, plant it for judging and for the finalize, and read its operation.

    The request must name the version after the table's latest and say so in the manifest's own ``version`` field: a
    put into an older slot a cleanup emptied would rewrite history, and a manifest whose version is not its slot's is
    one Lance refuses to read. The caller must :func:`discard` once judged, whatever the verdict.

    Raises:
        InvalidInputError: The manifest path is not a staged manifest under this table, it is not a Lance manifest, it
            cannot be opened, or its version is not the one after the table's latest.
    """
    staged = _staged_uri(location, request.manifest_path)
    latest = _latest_version(location, so)
    if request.version != latest + 1:
        raise InvalidInputError(f"commit refused: version {request.version} does not follow the table's latest version {latest}")
    if request.naming_scheme not in (None, "V2"):
        raise InvalidInputError(f"commit refused: the table's manifests are named V2, not {request.naming_scheme}")
    fs, _ = dataplane.dataset_fs(location, so)
    with fs.open_input_stream(dataplane.dataset_fs(staged, so)[1]) as source:
        manifest = source.read()
    stated = _varint_field(_manifest_message(manifest), _VERSION_FIELD)
    if stated != request.version:
        raise InvalidInputError(f"commit refused: the staged manifest is version {stated}, the request names {request.version}")
    detached_id, finalize_id = _DETACHED_BIT | secrets.randbits(62), _DETACHED_BIT | secrets.randbits(62)
    _write(location, so, f"d{detached_id}.manifest", with_manifest_version(manifest, detached_id))
    _write(location, so, f"d{finalize_id}.manifest", manifest)
    try:
        transaction = lance.dataset(location, version=detached_id, storage_options=dict(so) if so else None).read_transaction(detached_id)
    except (OSError, ValueError) as exc:
        _discard(location, so, detached_id, finalize_id)
        # A version Lance itself cannot open (an overlay on an appended fragment set reader flags 66, measured on
        # pylance 12.0.0) is the staged manifest's fault, never the catalog's.
        raise InvalidInputError(f"commit refused: the staged manifest cannot be opened: {exc}") from exc
    except BaseException:
        _discard(location, so, detached_id, finalize_id)
        raise
    operation = type(transaction.operation).__name__ if transaction is not None else "Unknown"
    return StagedVersion(location=location, detached_id=detached_id, finalize_id=finalize_id, staged_uri=staged, version=request.version, operation=operation)


def _open(staged: StagedVersion, so: StorageOptions) -> lance.LanceDataset:
    return lance.dataset(staged.location, version=staged.detached_id, storage_options=dict(so) if so else None)


#: The file version and row ids every table is created with (CLAUDE.md, "A Lance APPEND never names a file version").
_CREATE_FILE_VERSION = "2.2"

#: Manifest config a stock commit may not carry: Lance runs auto-cleanup inside the commit that finds it armed, which
#: deletes versions ([[LH-245]]).
_ARMED_CLEANUP_PREFIX = "lance.auto_cleanup."


class _Fragments(BaseModel):
    """The proposed version's fragments split against the version it follows."""

    new: list[InstanceOf[lance.FragmentMetadata]]
    carried: list[int]
    added_files: list[tuple[InstanceOf[lance.FragmentMetadata], InstanceOf[DataFile]]]


def _split(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> _Fragments:
    """New fragments, carried fragment ids, and the data files the proposed version adds.

    Raises:
        InvalidInputError: A new fragment names a data file the table already holds: re-referencing a committed file
            brings back rows a delete removed.
    """
    held_ids = {fragment.fragment_id for fragment in current.get_fragments()} if current is not None else set()
    held_paths = {data_file.path for fragment in current.get_fragments() for data_file in fragment.metadata.files} if current is not None else set()
    new: list[lance.FragmentMetadata] = []
    carried: list[int] = []
    added: list[tuple[lance.FragmentMetadata, DataFile]] = []
    for fragment in proposed.get_fragments():
        metadata = fragment.metadata
        if fragment.fragment_id in held_ids:
            carried.append(fragment.fragment_id)
        else:
            new.append(metadata)
            resurrected = [data_file.path for data_file in metadata.files if data_file.path in held_paths]
            if resurrected:
                raise InvalidInputError(f"commit refused: new fragment {fragment.fragment_id} names data file(s) the table already holds: {resurrected[:5]}")
        added.extend((metadata, data_file) for data_file in metadata.files if data_file.path not in held_paths)
    return _Fragments(new=new, carried=carried, added_files=added)


def _refuse_changed_invariants(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> None:
    """A create is 2.2 with stable row ids, and no later version changes either."""
    stable = bool(proposed.has_stable_row_ids)
    if current is None:
        if proposed.data_storage_version != _CREATE_FILE_VERSION or not stable:
            raise InvalidInputError(
                f"commit refused: a table is created at data_storage_version {_CREATE_FILE_VERSION} with stable row ids, "
                f"not {proposed.data_storage_version} with stable row ids {stable}"
            )
        return
    if proposed.data_storage_version != current.data_storage_version or stable != bool(current.has_stable_row_ids):
        raise InvalidInputError("commit refused: a commit may not change the table's data_storage_version or its stable row ids")


def _refuse_changed_schema_governance(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> None:
    """Reserved schema metadata, the provenance and key columns, and field-level ``rask.*`` metadata stay as they were.

    Each has its own door (``schema_metadata/update``, ``alter_columns``/``drop_columns``, ``update_field_metadata``)
    with its own rung, so a staged commit may not change them.
    """
    after = proposed.schema
    reserved_after = {key: value for key, value in (after.metadata or {}).items() if provenance_guard.is_reserved_key(key)}
    if current is None:
        provenance_guard.refuse_reserved_keys(reserved_after, door="version/create")
        return
    before = current.schema
    reserved_before = {key: value for key, value in (before.metadata or {}).items() if provenance_guard.is_reserved_key(key)}
    if reserved_after != reserved_before:
        raise InvalidInputError("commit refused: reserved schema metadata (lineage.*, rask.*) is changed only through schema_metadata/update")
    for path in provenance_guard.protected_paths(before):
        kept, was = _field_at(after, path), _field_at(before, path)
        if kept is None or was is None or not kept.equals(was, check_metadata=True):
            raise InvalidInputError(f"commit refused: provenance or key column {path!r} is changed or dropped; it is kept by every commit")
    if _governed_field_metadata(after) != _governed_field_metadata(before):
        raise InvalidInputError("commit refused: field metadata under rask.* is changed only through update_field_metadata")


def _field_at(schema: pa.Schema, path: str) -> pa.Field | None:
    head, _, rest = path.partition(".")
    if head not in schema.names:
        return None
    field = schema.field(head)
    for part in rest.split(".") if rest else []:
        if not pa.types.is_struct(field.type) or field.type.get_field_index(part) < 0:
            return None
        field = field.type.field(part)
    return field


def _governed_field_metadata(schema: pa.Schema) -> dict[str, dict[bytes, bytes]]:
    governed: dict[str, dict[bytes, bytes]] = {}

    def walk(field: pa.Field, prefix: str) -> None:
        path = f"{prefix}{field.name}"
        keys = {key: value for key, value in (field.metadata or {}).items() if key.startswith(b"rask.")}
        if keys:
            governed[path] = keys
        if pa.types.is_struct(field.type):
            for index in range(field.type.num_fields):
                walk(field.type.field(index), f"{path}.")

    for field in schema:
        walk(field, "")
    return governed


def _refuse_config(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> None:
    """No armed auto-cleanup, and the catalog's own ``rask.*`` config keys stay as the catalog wrote them."""
    after = dict(proposed.config())
    armed = sorted(key for key in after if key.startswith(_ARMED_CLEANUP_PREFIX))
    if armed:
        raise InvalidInputError(f"commit refused: manifest config arms Lance auto-cleanup ({armed}), which deletes versions inside a commit")
    before = dict(current.config()) if current is not None else {}
    if {k: v for k, v in after.items() if k.startswith("rask.")} != {k: v for k, v in before.items() if k.startswith("rask.")}:
        raise InvalidInputError("commit refused: rask.* table config is the catalog's own and is not changed by a commit")


def _refuse_bases(
    location: str, proposed: lance.LanceDataset, current: lance.LanceDataset | None, added: Sequence[tuple[lance.FragmentMetadata, DataFile]], judge: BaseJudge
) -> None:
    """The base list stays the table's own, and no added file resolves through a base ([[LH-209]], [[LH-279]])."""
    refs = features.manifest_base_path_refs(proposed)
    if current is not None and sorted(ref.model_dump_json() for ref in refs) != sorted(
        ref.model_dump_json() for ref in features.manifest_base_path_refs(current)
    ):
        raise InvalidInputError("commit refused: a commit may not add, remove or repoint the table's bases")
    require_sanctioned_bases(location, refs, judge=judge)
    based = [data_file.path for _, data_file in added if data_file.base_id is not None]
    if based:
        raise InvalidInputError(
            f"commit refused: {len(based)} added data file(s) resolve through a base, e.g. {based[:5]}; write them under the table's own data/"
        )


def _refuse_forged_files(location: str, so: StorageOptions, proposed: lance.LanceDataset, split: _Fragments) -> None:
    """Every added file is well formed, present at its declared size, and its footer agrees with its fragment ([[LH-211]])."""
    for index, (_, data_file) in enumerate(split.added_files):
        client_fragments.refuse_a_malformed_data_file(index, data_file)
    paths = [data_file.path for _, data_file in split.added_files]
    if len(set(paths)) < len(paths):
        raise InvalidInputError("commit refused: one data file is named twice; every field id it carries would be minted again")
    owners = {id(metadata): metadata for metadata, _ in split.added_files}
    dataplane.verify_fragment_data_files(
        location, so, [{"files": [{"path": data_file.path, "file_size_bytes": data_file.file_size_bytes}]} for _, data_file in split.added_files]
    )
    client_fragments.verify_against_footers(location, so, list(owners.values()), proposed)


def _refuse_held_keys(proposed: lance.LanceDataset, split: _Fragments) -> None:
    """No two rows of the new fragments, and no new row and carried row, share a value of the table's primary key ([[LH-243]])."""
    keys = primary_keys.key_columns(proposed.lance_schema)
    if not keys or not split.new:
        return
    columns = list(dict.fromkeys(key.split(".", 1)[0] for key in keys))
    new_ids = {metadata.id for metadata in split.new}
    rows = proposed.scanner(columns=columns, fragments=[fragment for fragment in proposed.get_fragments() if fragment.fragment_id in new_ids]).to_table()
    primary_keys.refuse_repeats_within(rows, keys, door="version/create")
    if not split.carried or rows.num_rows == 0 or not primary_keys.joinable(rows, keys):
        return
    carried = [fragment for fragment in proposed.get_fragments() if fragment.fragment_id in set(split.carried)]
    first = keys[0]
    held = proposed.scanner(columns=list(keys), fragments=carried, filter=pc.field(first).isin(rows.column(first).unique())).to_table()
    if held.num_rows == 0:
        return
    shared = rows.select(list(keys)).join(held.select(list(keys)), keys=list(keys), join_type="inner")
    if shared.num_rows:
        raise InvalidInputError(
            f"commit refused: the table already holds a row with primary key {shared.slice(0, 1).to_pylist()[0]}; change it with merge_insert or update"
        )


def _refuse_added_feature_flags(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> None:
    """Refuse a feature flag the commit turns on that this catalog does not serve.

    Runs after the file-version rule, which names the files that would set flag 256 rather than only the flag. A flag
    the table already carries is the table's, not the commit's: a bronze table created with an external blob base has
    flag 16 from its first version (measured on pylance 13.0.0), and every honest append to it carries it on.
    """
    reader, writer = features.manifest_feature_flags(proposed)
    if current is not None:
        held_reader, held_writer = features.manifest_feature_flags(current)
        reader, writer = reader & ~held_reader, writer & ~held_writer
    reason = features.describe_unsupported_flags(reader, writer)
    if reason is not None:
        raise UnsupportedOperationError(f"commit refused: {reason}")


#: ``uint64 next_row_id = 14`` of ``message Manifest`` (Lance ``protos/table.proto`` at v13.0.0).
_NEXT_ROW_ID_FIELD = 14


def _varint_field(message: bytes, number: int) -> int | None:
    """The value of top-level varint field ``number`` in ``message``, or ``None`` when it is absent (proto3's zero)."""
    at, found = 0, None
    while at < len(message):
        key, at = _read_varint(message, at)
        wire = key & 0x7
        if wire == 0:
            value, at = _read_varint(message, at)
            if key >> 3 == number:
                found = value
        elif wire == 1:
            at += 8
        elif wire == 2:
            length, at = _read_varint(message, at)
            at += length
        elif wire == 5:
            at += 4
        else:
            raise InvalidInputError(f"a Manifest message has an unsupported wire type {wire}")
    return found


def _manifest_message(manifest: bytes) -> bytes:
    if len(manifest) < _FOOTER.size or manifest[-4:] != _MAGIC:
        raise InvalidInputError("not a Lance manifest (no LANC footer)")
    offset = _FOOTER.unpack(manifest[-_FOOTER.size :])[0]
    (length,) = _LENGTH.unpack_from(manifest, offset)
    return manifest[offset + _LENGTH.size : offset + _LENGTH.size + length]


def _next_row_id(location: str, so: StorageOptions, version: int) -> int:
    """The first stable row id the table had not assigned at ``version``, read off its committed manifest.

    pylance exposes no ``next_row_id``. A V2-named manifest is ``_versions/<u64::MAX - version>.manifest`` and a V1-named
    one ``_versions/<version>.manifest`` (``lance_docs/file_format.md``).
    """
    fs, base = dataplane.dataset_fs(location, so)
    for name in (f"{(1 << 64) - 1 - version}.manifest", f"{version}.manifest"):
        try:
            with fs.open_input_stream(f"{base.rstrip('/')}/_versions/{name}") as source:
                return _varint_field(_manifest_message(source.read()), _NEXT_ROW_ID_FIELD) or 0
        except FileNotFoundError:
            continue
    raise InvalidInputError(f"the version this commit follows, {version}, has no committed manifest")


def _refuse_malformed_new_fragments(
    staged: StagedVersion, so: StorageOptions, proposed: lance.LanceDataset, current: lance.LanceDataset | None, split: _Fragments
) -> None:
    """Every new fragment names a data file, and an appended one carries no overlay or deletion file.

    Measured through the client-direct append on pylance 12.0.0, each of these committed: a fragment with no data file,
    an overlay (the table no longer opened) and a ``row_id_meta`` copied from a committed fragment (two rows with one
    stable row id, judged by :func:`_refuse_rows_coming_back`).
    """
    empty = [metadata.id for metadata in split.new if not metadata.files]
    if empty:
        raise InvalidInputError(f"commit refused: fragment(s) {empty[:5]} name no data file")
    if staged.operation != "Append" or current is None:
        return
    carrying = [metadata.id for metadata in split.new if metadata.overlays or metadata.deletion_file is not None]
    if carrying:
        raise InvalidInputError(f"commit refused: appended fragment(s) {carrying[:5]} carry an overlay or a deletion file, which an append never writes")


#: Operations whose rung is above the writer's: they rewind, rewrite or replace the table, so moving rows and their
#: ids is what they are for (``fga_deps.require_staged_operation``).
_REWRITING_OPERATIONS = frozenset({"Restore", "Rewrite", "Overwrite"})


def _row_ids(dataset: lance.LanceDataset, fragment_ids: set[int]) -> list[int]:
    fragments = [fragment for fragment in dataset.get_fragments() if fragment.fragment_id in fragment_ids]
    if not fragments:
        return []
    return dataset.scanner(columns=[], with_row_id=True, fragments=fragments).to_table().column("_rowid").to_pylist()


def _refuse_rows_coming_back(staged: StagedVersion, so: StorageOptions, proposed: lance.LanceDataset, current: lance.LanceDataset, split: _Fragments) -> None:
    """At a writer's rung, no deleted row comes back and every new row's id is fresh or one this commit deleted.

    Judged on the fragments, never on the operation the writer's transaction file names: relabelled, a restore is an
    "Append" whose rows reuse old ids, and an undelete an "Update" whose kept fragment lost its deletion file. An
    Update legitimately moves a row with its stable row id, so an id the commit deleted may reappear once.
    """
    held = {fragment.fragment_id: fragment.metadata for fragment in current.get_fragments()}
    kept = {fragment.fragment_id: fragment.metadata for fragment in proposed.get_fragments() if fragment.fragment_id in held}
    touched = {fid for fid, metadata in kept.items() if str(metadata.deletion_file) != str(held[fid].deletion_file)}
    before = set(_row_ids(current, touched | (set(held) - set(kept))))
    after = set(_row_ids(proposed, touched))
    if after - before:
        raise InvalidInputError("commit refused: rows a delete removed come back; restore a version through the restore door")
    deleted = before - after
    added = _row_ids(proposed, {metadata.id for metadata in split.new})
    if len(set(added)) < len(added):
        raise InvalidInputError("commit refused: two new rows carry one stable row id")
    floor = _next_row_id(staged.location, so, staged.version - 1)
    if {row_id for row_id in added if row_id < floor} - deleted:
        raise InvalidInputError(f"commit refused: new rows reuse stable row ids below the table's next row id {floor} that this commit did not delete")


def judge(staged: StagedVersion, so: StorageOptions, *, base_judge: BaseJudge, external_bases: Sequence[base_registry.RecordedBase]) -> None:
    """Refuse the staged version if it breaks a rule the catalog's own write doors hold.

    The version is compared with the one it follows. What it adds is judged: a file or a row carried over was judged
    when it landed.

    Raises:
        InvalidInputError: The version breaks one of the rules below; nothing was finalized.
        UnsupportedOperationError: The version needs a Lance feature this catalog does not serve.
    """
    proposed = _open(staged, so)
    current = None if staged.creates else lance.dataset(staged.location, version=staged.version - 1, storage_options=dict(so) if so else None)
    _refuse_changed_invariants(proposed, current)
    _refuse_changed_schema_governance(proposed, current)
    _refuse_config(proposed, current)
    split = _split(proposed, current)
    _refuse_bases(staged.location, proposed, current, split.added_files, base_judge)
    reason = features.describe_foreign_data_file_versions(proposed.data_storage_version, [data_file for _, data_file in split.added_files])
    if reason is not None:
        raise InvalidInputError(f"commit refused: {reason}")
    _refuse_added_feature_flags(proposed, current)
    _refuse_malformed_new_fragments(staged, so, proposed, current, split)
    if current is not None and proposed.has_stable_row_ids and staged.operation not in _REWRITING_OPERATIONS:
        _refuse_rows_coming_back(staged, so, proposed, current, split)
    if split.added_files:
        _refuse_forged_files(staged.location, so, proposed, split)
    columns = client_fragments.blob_columns(proposed)
    if columns and split.new:
        client_fragments.verify_blob_sidecars(proposed, split.new, columns, external_bases=external_bases, object_sizes=partial(dataplane.object_sizes, so=so))
    _refuse_held_keys(proposed, split)
    if staged.operation == "Rewrite" and current is not None:
        maintenance.require_compactable(current, so)


#: The lineage operation each staged Lance operation is announced as. pylance stages merge_insert as an ``Update`` and
#: add_columns as a ``Merge`` (measured on 13.0.0), so each maps to the door that writes the same transaction.
LINEAGE_OPERATION: dict[str, str] = {
    "Append": lineage_emit.INSERT,
    "Delete": lineage_emit.DELETE,
    "Update": lineage_emit.UPDATE,
    "DataReplacement": lineage_emit.UPDATE,
    "DataOverlay": lineage_emit.UPDATE,
    "Merge": lineage_emit.ADD_COLUMNS,
    "Project": lineage_emit.DROP_COLUMNS,
    "CreateIndex": lineage_emit.CREATE_INDEX,
    "UpdateConfig": lineage_emit.UPDATE_SCHEMA_METADATA,
    "Restore": lineage_emit.RESTORE_TABLE,
    "Rewrite": lineage_emit.COMPACT_TABLE,
    "Overwrite": lineage_emit.OVERWRITE_TABLE,
}


def lineage_operation(staged: StagedVersion) -> str:
    """The lineage operation the finalized version is announced as; a table's first version is its create."""
    return lineage_emit.CREATE_TABLE if staged.creates else LINEAGE_OPERATION[staged.operation]


def _discard(location: str, so: StorageOptions, *ids: int) -> None:
    for planted in ids:
        dataplane.discard_detached_manifest(location, so, planted)


def discard(staged: StagedVersion, so: StorageOptions) -> None:
    """Delete the copy :func:`stage` planted for judging; the client's staged manifest stays the client's."""
    _discard(staged.location, so, staged.detached_id)


def refuse(staged: StagedVersion, so: StorageOptions) -> None:
    """Delete the finalize copy of a version the judge refused."""
    _discard(staged.location, so, staged.finalize_id)


def finalize(ns: LanceNamespace, staged: StagedVersion, request: CreateTableVersionRequest, so: StorageOptions) -> CreateTableVersionResponse:
    """Finalize the judged bytes through the backend's put-if-not-exists; a lost race is the backend's 409.

    The backend is handed the catalog's own copy, which it moves into the version slot (measured on pylance 13.0.0), so
    the version that lands is the one judged whatever the writer puts over its staged key meanwhile.
    """
    path = f"{_object_store_path(staged.location.rstrip('/'))}/_versions/d{staged.finalize_id}.manifest"
    judged = request.model_copy(update={"manifest_path": path})
    try:
        response = CreateTableVersionResponse.model_validate(native.call(ns, "create_table_version", judged))
    except BaseException:
        refuse(staged, so)
        raise
    _delete_staged(staged, so)
    return response


def _delete_staged(staged: StagedVersion, so: StorageOptions) -> None:
    """Delete the writer's staged manifest once its version landed."""
    dataplane.delete_table_object(staged.location, so, f"_versions/{staged.staged_uri.rsplit('/', 1)[-1]}")
