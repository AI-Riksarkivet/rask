"""The catalog's ``CreateTableVersion``: judge a client-staged manifest, then finalize it ([[LH-471]], ADR 0157).

Under ``managed_versioning`` a stock writer puts its data, transaction and deletion files and a staged
``_versions/<n>.manifest-<uuid>`` (under ``tree/<branch>/`` for a commit on a branch, [[LH-490]]), and asks the catalog
to finalize it. The catalog judges the version that manifest describes before anything is final, so a refusal leaves
the ref at its version.

pylance opens no staged manifest, but it opens a DETACHED one: ``_versions/d<id>.manifest`` with the top bit of ``id``
set is read by ``lance.dataset(location, version=id)`` and never becomes the latest version. The staged manifest is
copied there with one change, its ``version`` field set to ``id``: Lance refuses a manifest whose own version is not
the one asked for (measured on pylance 13.0.0, "Version not found" for an unchanged copy), and a create has no
committed version to ``checkout_version`` from. The copy is judged through pylance and deleted whatever the verdict. The finalize is
the backend's own put-if-not-exists (``create_table_version`` of the ``dir`` namespace), so two writers racing for one
version number meet the store's arbitration, not the catalog's.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from lance.fragment import DataFile
from lance_namespace import (
    CreateTableVersionRequest,
    CreateTableVersionResponse,
    InvalidInputError,
    ServiceUnavailableError,
    TableBranchNotFoundError,
    UnsupportedOperationError,
)
from pydantic import BaseModel, InstanceOf

from catalog.core import encoding, formats, lineage_emit, manifest_bytes, provenance_guard
from catalog.core.base_judge import BaseJudge, require_sanctioned_bases
from catalog.services import client_fragments, committed_files, dataplane, maintenance, primary_keys, table_claims
from service_kit.lakehouse import base_refs, base_registry, features, file_identities, maintenance_due
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lancekit.outage import reads_as_store_outage


log = logging.getLogger(__name__)


class CommitRef(BaseModel):
    """The ref a stock client's staged commit continues: main, or a branch of the table rooted at ``table_location``.

    A branch keeps its own version history, data, transaction, deletion and index files under ``tree/<name>/``
    (``lance_docs/lance/format/table/branch_tag.md`` § Branch Dataset Layout; measured on pylance 13.0.0, a stock client stages a branch
    commit there and names the branch in ``CreateTableVersion``), and reads its parent's files through bases on the
    table's root.
    """

    table_location: str
    branch: str | None = None

    @property
    def location(self) -> str:
        """Where the ref's own versions and files live: the table's root, or its ``tree/<branch>``."""
        root = self.table_location.rstrip("/")
        return root if self.branch is None else f"{root}/{BRANCH_CONTAINER}/{self.branch}"


def commit_ref(table_location: str, so: StorageOptions, branch: str | None) -> CommitRef:
    """The ref ``branch`` names on the table rooted at ``table_location``: main for ``None``, else a branch it lists.

    A branch is created through ``CreateTableBranch``, which writes its ref; a stock client's own ``create_branch``
    commits a ``Clone`` here and then writes ``_refs/branches/<name>.json`` itself (Lance ``rust/lance/src/dataset.rs``
    ``create_branch`` at v13.0.0), and no writer is vended a put under ``_refs/``.

    Raises:
        TableBranchNotFoundError: The table lists no branch named ``branch``.
        ServiceUnavailableError: The object store did not answer, which says nothing about the table's branches.
    """
    if branch is None:
        return CommitRef(table_location=table_location)
    if branch not in _listed_branches(table_location, so):
        raise TableBranchNotFoundError(
            f"branch {branch!r} does not exist: a branch is created through CreateTableBranch (POST /v1/table/{{id}}/branches/create), "
            "and each later version of it is committed here"
        )
    return CommitRef(table_location=table_location, branch=branch)


def _listed_branches(table_location: str, so: StorageOptions) -> set[str]:
    """The branches the table lists; none when it has no readable dataset, from which no branch was ever taken.

    A store that did not answer raises rather than reading as no branches ([[LH-247]]): pylance reports it as the same
    bare error as an absent dataset.
    """
    try:
        return set(lance.dataset(table_location, storage_options=so.in_clear() or None).branches.list())
    except (OSError, ValueError) as exc:
        if reads_as_store_outage(exc):
            raise ServiceUnavailableError("the object store did not answer listing the table's branches") from exc
        return set()


class StagedVersion(BaseModel):
    """A staged manifest planted as a detached version, and what its transaction says."""

    #: The ref's own location (:attr:`CommitRef.location`), where its versions and the files it adds live.
    location: str
    #: The table's root, which the ref's bases are judged against.
    table_location: str
    #: ``None`` for main.
    branch: str | None = None
    detached_id: int
    #: The catalog-written copy the backend finalizes from: the writer can put over its own staged key until the
    #: finalize, and the write tier's explicit Deny on ``_versions/*.manifest`` keeps it off this one.
    finalize_id: int
    #: The writer's staged manifest, which the backend no longer consumes: deleted once the version lands.
    staged_uri: str
    version: int
    operation: str
    #: The staged transaction's properties, where a stock writer names its run (:mod:`catalog.core.run_context`).
    properties: dict[str, str] = {}
    #: The staged transaction's own uuid, which keys the author the catalog records for a commit naming none ([[LH-497]]).
    transaction_uuid: str = ""
    #: The version the staged transaction was planned on, which names its transaction file.
    read_version: int = 0
    #: The staged manifest's ``max_fragment_id``, ``None`` when it states none.
    max_fragment_id: int | None = None

    @property
    def creates(self) -> bool:
        """The table's first version: there is no committed version for it to follow."""
        return self.branch is None and self.version == 1


def _staged_uri(location: str, manifest_path: str) -> str:
    """The staged manifest's URI, refusing any path but one staged name directly under the ref's ``_versions/``.

    pylance sends ``manifest_path`` as an object-store path (measured on 13.0.0: the ref's path with no scheme and
    no leading slash, then ``_versions/<n>.manifest-<uuid>``), so it is compared in that form.
    """
    root = location.rstrip("/")
    prefix = f"{dataplane.object_store_path(root)}/_versions/"
    name = manifest_path.removeprefix(prefix) if manifest_path.startswith(prefix) else ""
    if not name or "/" in name or ".manifest-" not in name or name.endswith(".manifest"):
        raise InvalidInputError(f"manifest_path {manifest_path!r} is not a staged manifest under the ref's _versions/")
    return f"{root}/_versions/{name}"


def _latest_version(location: str, so: StorageOptions) -> int:
    """The table's latest committed version, 0 for a declared table with none."""
    try:
        return int(lance.dataset(location, storage_options=so.in_clear() or None).version)
    except (OSError, ValueError):
        return 0


def _write(location: str, so: StorageOptions, name: str, body: bytes) -> None:
    fs, base = dataplane.dataset_fs(location, so)
    with fs.open_output_stream(f"{base.rstrip('/')}/_versions/{name}") as sink:
        sink.write(body)


def stage(ref: CommitRef, so: StorageOptions, request: CreateTableVersionRequest) -> StagedVersion:
    """Read the staged manifest ``request`` names once, plant it for judging and for the finalize, and read its operation.

    The request must name the version after the ref's latest and say so in the manifest's own ``version`` field: a
    put into an older slot a cleanup emptied would rewrite history, and a manifest whose version is not its slot's is
    one Lance refuses to read. The caller must :func:`discard` once judged, whatever the verdict.

    Raises:
        InvalidInputError: The manifest path is not a staged manifest under the ref, it is not a Lance manifest, it
            cannot be opened, or its version is not the one after the ref's latest.
    """
    location = ref.location
    staged = _staged_uri(location, request.manifest_path)
    latest = _latest_version(location, so)
    if request.version != latest + 1:
        raise InvalidInputError(f"commit refused: version {request.version} does not follow the latest version {latest}")
    if request.naming_scheme not in (None, "V2"):
        raise InvalidInputError(f"commit refused: the table's manifests are named V2, not {request.naming_scheme}")
    fs, _ = dataplane.dataset_fs(location, so)
    with fs.open_input_stream(dataplane.dataset_fs(staged, so)[1]) as source:
        manifest = source.read()
    stated = manifest_bytes.varint_field(manifest_bytes.manifest_message(manifest), manifest_bytes.VERSION_FIELD)
    if stated != request.version:
        raise InvalidInputError(f"commit refused: the staged manifest is version {stated}, the request names {request.version}")
    detached_id, finalize_id = manifest_bytes.detached_id(), manifest_bytes.detached_id()
    _write(location, so, f"d{detached_id}.manifest", manifest_bytes.with_manifest_version(manifest, detached_id))
    _write(location, so, f"d{finalize_id}.manifest", manifest)
    try:
        transaction = lance.dataset(location, version=detached_id, storage_options=so.in_clear() or None).read_transaction(detached_id)
    except (OSError, ValueError) as exc:
        _discard(location, so, detached_id, finalize_id)
        # A version Lance itself cannot open (an overlay on an appended fragment set reader flags 66, measured on
        # pylance 12.0.0) is the staged manifest's fault, never the catalog's.
        raise InvalidInputError(f"commit refused: the staged manifest cannot be opened: {exc}") from exc
    except BaseException:
        _discard(location, so, detached_id, finalize_id)
        raise
    operation = type(transaction.operation).__name__ if transaction is not None else "Unknown"
    if operation == lance.LanceOperation.BaseOperation.__name__:
        operation = _unmodelled_operation(manifest) or operation
    properties = dict(transaction.transaction_properties or {}) if transaction is not None else {}
    return StagedVersion(
        location=location,
        table_location=ref.table_location,
        branch=ref.branch,
        detached_id=detached_id,
        finalize_id=finalize_id,
        staged_uri=staged,
        version=request.version,
        operation=operation,
        properties=properties,
        transaction_uuid=transaction.uuid if transaction is not None else "",
        read_version=transaction.read_version if transaction is not None else 0,
        max_fragment_id=manifest_bytes.varint_field(manifest_bytes.manifest_message(manifest), manifest_bytes.MAX_FRAGMENT_ID_FIELD),
    )


def _unmodelled_operation(manifest: bytes) -> str | None:
    """The operation of ``manifest``'s inline transaction, read off its protobuf, when pylance models no class for it.

    A stock compaction commits a ``ReserveFragments`` before its ``Rewrite`` (``lance_docs/lance/format/table/transaction.md`` § Rewrite),
    and pylance reads it back as ``BaseOperation``. ``None`` when the manifest carries no inline transaction or names
    another operation, which then stays unnamed and is refused.
    """
    inline = manifest_bytes.transaction_section(manifest)
    if inline is None:
        return None
    return manifest_bytes.operation_name(inline)


def _open(staged: StagedVersion, so: StorageOptions) -> lance.LanceDataset:
    return lance.dataset(staged.location, version=staged.detached_id, storage_options=so.in_clear() or None)


#: The file version and row ids every table is created with (CLAUDE.md, "A Lance APPEND never names a file version").
_CREATE_FILE_VERSION = "2.2"

#: Manifest config a stock commit may not carry: Lance runs auto-cleanup inside the commit that finds it armed, which
#: deletes versions ([[LH-245]]).
_ARMED_CLEANUP_PREFIX = "lance.auto_cleanup."


class _Fragments(BaseModel):
    """The proposed version's fragments split against the version it follows."""

    new: list[InstanceOf[lance.FragmentMetadata]]
    added_files: list[tuple[InstanceOf[lance.FragmentMetadata], InstanceOf[DataFile]]]


def _split(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> _Fragments:
    """New fragments and the data files the proposed version adds.

    Raises:
        InvalidInputError: A new fragment names a data file the table already holds: re-referencing a committed file
            brings back rows a delete removed.
    """
    held_paths = {data_file.path for fragment in current.get_fragments() for data_file in fragment.metadata.files} if current is not None else set()
    new, carried = primary_keys.fragments_since(proposed, current)
    for fragment in new:
        resurrected = [data_file.path for data_file in fragment.metadata.files if data_file.path in held_paths]
        if resurrected:
            raise InvalidInputError(f"commit refused: new fragment {fragment.fragment_id} names data file(s) the table already holds: {resurrected[:5]}")
    added = [(fragment.metadata, data_file) for fragment in [*new, *carried] for data_file in fragment.metadata.files if data_file.path not in held_paths]
    return _Fragments(new=[fragment.metadata for fragment in new], added_files=added)


class CloneEvidence(BaseModel):
    """The maintenance sweep's clone list, and how old it may be, as a compaction is judged against it."""

    clones: maintenance_due.CloneList
    max_age: timedelta


class _Judged(BaseModel):
    """A staged version beside the version it follows: what every rule of :func:`judge` reads."""

    staged: StagedVersion
    so: StorageOptions
    proposed: InstanceOf[lance.LanceDataset]
    #: ``None`` for the table's first version, which follows none.
    current: InstanceOf[lance.LanceDataset] | None
    split: _Fragments
    #: Read for a compaction and for no other operation; ``None`` when no sweep has written a clone list.
    clone_evidence: CloneEvidence | None = None

    @property
    def followed(self) -> lance.LanceDataset:
        """The version this one follows, for a rule that judges a change against it.

        Raises:
            InvalidInputError: The version is the table's first, so it changes nothing that came before.
        """
        if self.current is None:
            raise InvalidInputError(f"commit refused: a {self.staged.operation} needs a version to follow, and this is the table's first")
        return self.current

    @property
    def adds_data(self) -> bool:
        """It names a data file the version it follows does not hold, and is not a compaction or a restore."""
        return bool(self.split.added_files) and self.staged.operation not in _CARRIES_HELD_ROWS


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
    if provenance_guard.field_labels(after) != provenance_guard.field_labels(before):
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


def _refuse_config(proposed: lance.LanceDataset, current: lance.LanceDataset | None) -> None:
    """No armed auto-cleanup, no ``lance.compaction.*`` key ([[LH-538]]), and the catalog's own ``rask.*`` config keys
    stay as the catalog wrote them."""
    after = dict(proposed.config())
    armed = sorted(key for key in after if key.startswith(_ARMED_CLEANUP_PREFIX))
    if armed:
        raise InvalidInputError(f"commit refused: manifest config arms Lance auto-cleanup ({armed}), which deletes versions inside a commit")
    if steering := formats.compaction_config_keys(after):
        raise InvalidInputError(
            f"commit refused: manifest config carries {steering}, which Lance would plan a compaction from in place of the table's maintenance policy"
        )
    before = dict(current.config()) if current is not None else {}
    if {k: v for k, v in after.items() if k.startswith("rask.")} != {k: v for k, v in before.items() if k.startswith("rask.")}:
        raise InvalidInputError("commit refused: rask.* table config is the catalog's own and is not changed by a commit")


def _refuse_bases(version: _Judged, judge: BaseJudge) -> None:
    """The base list stays the table's own, and no added file resolves through a base ([[LH-209]], [[LH-279]])."""
    refs = features.manifest_base_path_refs(version.proposed)
    if version.current is not None and sorted(ref.model_dump_json() for ref in refs) != sorted(
        ref.model_dump_json() for ref in features.manifest_base_path_refs(version.current)
    ):
        raise InvalidInputError("commit refused: a commit may not add, remove or repoint the table's bases")
    require_sanctioned_bases(version.staged.table_location, refs, judge=judge)
    based = [data_file.path for _, data_file in version.split.added_files if data_file.base_id is not None]
    if based:
        raise InvalidInputError(
            f"commit refused: {len(based)} added data file(s) resolve through a base, e.g. {based[:5]}; write them under the table's own data/"
        )


def _refuse_foreign_file_versions(version: _Judged) -> None:
    """Every added data file is at the table's file version, which an append never names (reader flag 256 otherwise)."""
    added = [data_file for _, data_file in version.split.added_files]
    reason = features.describe_foreign_data_file_versions(version.proposed.data_storage_version, added)
    if reason is not None:
        raise InvalidInputError(f"commit refused: {reason}")


def _refuse_forged_files(version: _Judged) -> None:
    """Every added file is well formed, present at its declared size, and its footer agrees with its fragment ([[LH-211]])."""
    added = version.split.added_files
    if not added:
        return
    for index, (_, data_file) in enumerate(added):
        client_fragments.refuse_a_malformed_data_file(index, data_file)
    paths = [data_file.path for _, data_file in added]
    if len(set(paths)) < len(paths):
        raise InvalidInputError("commit refused: one data file is named twice; every field id it carries would be minted again")
    owners = {id(metadata): metadata for metadata, _ in added}
    location, so = version.staged.location, version.so
    dataplane.verify_fragment_data_files(
        location, so, [{"files": [{"path": data_file.path, "file_size_bytes": data_file.file_size_bytes}]} for _, data_file in added]
    )
    client_fragments.verify_against_footers(location, so, list(owners.values()), version.proposed)


def _refuse_unverified_blobs(version: _Judged, external_bases: Sequence[base_registry.RecordedBase]) -> None:
    """Every blob a new fragment names is present, under the table's own root or a base its record holds."""
    columns = client_fragments.blob_columns(version.proposed)
    if not columns or not version.split.new:
        return
    client_fragments.verify_blob_sidecars(
        version.proposed,
        version.split.new,
        columns,
        external_bases=external_bases,
        object_sizes=partial(dataplane.object_sizes, so=version.so),
        sidecar_sizes=partial(dataplane.data_object_sizes, version.staged.location, so=version.so),
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


def _committed_field(location: str, so: StorageOptions, version: int, field: int) -> int:
    """A varint field of the table's committed manifest at ``version``, 0 when the manifest leaves it unset.

    pylance exposes neither ``next_row_id`` nor ``max_fragment_id``. A V2-named manifest is
    ``_versions/<u64::MAX - version>.manifest`` and a V1-named one ``_versions/<version>.manifest`` (``lance_docs/lance/format/table/transaction.md``).
    """
    fs, base = dataplane.dataset_fs(location, so)
    for name in (f"{(1 << 64) - 1 - version}.manifest", f"{version}.manifest"):
        try:
            with fs.open_input_stream(f"{base.rstrip('/')}/_versions/{name}") as source:
                return manifest_bytes.varint_field(manifest_bytes.manifest_message(source.read()), field) or 0
        except FileNotFoundError:
            continue
    raise InvalidInputError(f"the version this commit follows, {version}, has no committed manifest")


def _refuse_malformed_new_fragments(version: _Judged) -> None:
    """Every new fragment names a data file, and an appended one carries no overlay or deletion file.

    Measured through the client-direct append on pylance 12.0.0, each of these committed: a fragment with no data file,
    an overlay (the table no longer opened) and a ``row_id_meta`` copied from a committed fragment (two rows with one
    stable row id, judged by :func:`_refuse_rows_coming_back`).
    """
    empty = [metadata.id for metadata in version.split.new if not metadata.files]
    if empty:
        raise InvalidInputError(f"commit refused: fragment(s) {empty[:5]} name no data file")
    if version.staged.operation != "Append" or version.current is None:
        return
    carrying = [metadata.id for metadata in version.split.new if metadata.overlays or metadata.deletion_file is not None]
    if carrying:
        raise InvalidInputError(f"commit refused: appended fragment(s) {carrying[:5]} carry an overlay or a deletion file, which an append never writes")


#: A compaction's two commits: the ``ReserveFragments`` and the ``Rewrite`` that uses the ids it reserved.
COMPACTION_OPERATIONS = frozenset({"ReserveFragments", "Rewrite"})

#: Operations whose new files carry only rows the table held at some version, so they add no data a run produced.
_CARRIES_HELD_ROWS = frozenset({"Rewrite", "Restore"})

#: Operations whose rung is above the writer's: they rewind, rewrite or replace the table, so moving rows and their
#: ids is what they are for (``fga_deps.require_staged_operation``).
_REWRITING_OPERATIONS = frozenset({"Restore", "Rewrite", "Overwrite"})


def _row_ids(dataset: lance.LanceDataset, fragment_ids: set[int]) -> list[int]:
    fragments = [fragment for fragment in dataset.get_fragments() if fragment.fragment_id in fragment_ids]
    if not fragments:
        return []
    return dataset.scanner(columns=[], with_row_id=True, fragments=fragments).to_table().column("_rowid").to_pylist()


def _refuse_rows_coming_back(version: _Judged) -> None:
    """At a writer's rung, no deleted row comes back and every new row's id is fresh or one this commit deleted.

    Judged on the fragments, never on the operation the writer's transaction file names: relabelled, a restore is an
    "Append" whose rows reuse old ids, and an undelete an "Update" whose kept fragment lost its deletion file. An
    Update legitimately moves a row with its stable row id, so an id the commit deleted may reappear once.
    """
    current, proposed = version.current, version.proposed
    if current is None or not proposed.has_stable_row_ids or version.staged.operation in _REWRITING_OPERATIONS:
        return
    held = {fragment.fragment_id: fragment.metadata for fragment in current.get_fragments()}
    kept = {fragment.fragment_id: fragment.metadata for fragment in proposed.get_fragments() if fragment.fragment_id in held}
    touched = {fid for fid, metadata in kept.items() if str(metadata.deletion_file) != str(held[fid].deletion_file)}
    before = set(_row_ids(current, touched | (set(held) - set(kept))))
    after = set(_row_ids(proposed, touched))
    if after - before:
        raise InvalidInputError("commit refused: rows a delete removed come back; restore a version through the restore door")
    deleted = before - after
    added = _row_ids(proposed, {metadata.id for metadata in version.split.new})
    if len(set(added)) < len(added):
        raise InvalidInputError("commit refused: two new rows carry one stable row id")
    floor = _next_row_id(version.staged.location, version.so, version.staged.version - 1)
    if {row_id for row_id in added if row_id < floor} - deleted:
        raise InvalidInputError(f"commit refused: new rows reuse stable row ids below the table's next row id {floor} that this commit did not delete")


def _next_row_id(location: str, so: StorageOptions, version: int) -> int:
    """The first stable row id the table had not assigned at ``version``."""
    return _committed_field(location, so, version, _NEXT_ROW_ID_FIELD)


def _fragment_json(fragment: lance.FragmentMetadata) -> str:
    """Everything Lance records for one fragment: its files, deletion file, overlays, and row id and version metadata."""
    return json.dumps(fragment.to_json(), sort_keys=True)


def _fragments_by_id(dataset: lance.LanceDataset) -> dict[int, str]:
    return {fragment.fragment_id: _fragment_json(fragment.metadata) for fragment in dataset.get_fragments()}


def _index_uuids(dataset: lance.LanceDataset) -> set[str]:
    return {str(index["uuid"]) for index in dataset.list_indices()}


def _refuse_changed_data(version: _Judged) -> None:
    """A ``CreateIndex`` or a ``ReserveFragments`` carries no data: it changes no fragment and no column."""
    before, after = _fragments_by_id(version.followed), _fragments_by_id(version.proposed)
    changed = sorted(fid for fid in before.keys() | after.keys() if before.get(fid) != after.get(fid))
    if changed:
        raise InvalidInputError(f"commit refused: a {version.staged.operation} changes no fragment, and this one changes fragment(s) {changed[:5]}")
    if not version.proposed.schema.equals(version.followed.schema, check_metadata=True):
        raise InvalidInputError(f"commit refused: a {version.staged.operation} changes no column and no schema metadata")


def _refuse_a_reservation_beyond_the_table(version: _Judged) -> None:
    """A ``ReserveFragments`` reserves fragment ids for the ``Rewrite`` after it and changes nothing else.

    Every fragment a rewrite writes holds at least one row it rewrites, so the table's physical rows bound how many ids
    one may need; ``max_fragment_id`` at ``2^32 - 1`` exhausts the id space and leaves the table unwritable
    (``lance_docs/lance/format/table/transaction.md`` § ReserveFragments).
    """
    _refuse_changed_data(version)
    if _index_uuids(version.proposed) != _index_uuids(version.followed):
        raise InvalidInputError("commit refused: a ReserveFragments adds or removes no index")
    held = _committed_field(version.staged.location, version.so, version.staged.version - 1, manifest_bytes.MAX_FRAGMENT_ID_FIELD)
    reserved = (version.staged.max_fragment_id or 0) - held
    rows = sum(fragment.metadata.physical_rows or 0 for fragment in version.followed.get_fragments())
    if not 1 <= reserved <= rows:
        raise InvalidInputError(f"commit refused: a ReserveFragments reserves between 1 and the table's {rows} rows of fragment ids, not {reserved}")
    _refuse_a_compaction_others_read_through(version)


def _refuse_a_rewrite_that_changes_rows(version: _Judged) -> None:
    """A ``Rewrite`` replaces whole fragments with fresh ones holding exactly their live rows, no fewer and no more.

    Measured on pylance 13.0.0, Lance commits a rewrite naming a fragment whose rows it does not carry (they are gone)
    and one rewriting the rows of a fragment it leaves in place (they are there twice, under one stable row id).
    """
    before, after = _fragments_by_id(version.followed), _fragments_by_id(version.proposed)
    in_place = sorted(fid for fid in before.keys() & after.keys() if before[fid] != after[fid])
    if in_place:
        raise InvalidInputError(f"commit refused: a Rewrite replaces fragments whole, and this one changes fragment(s) {in_place[:5]} in place")
    reused = [metadata.id for metadata in version.split.new if metadata.deletion_file is not None or metadata.overlays]
    if reused:
        raise InvalidInputError(f"commit refused: a Rewrite writes its fragments afresh, and new fragment(s) {reused[:5]} carry a deletion file or an overlay")
    _refuse_rows_not_carried(version, replaced=before.keys() - after.keys(), written=after.keys() - before.keys())
    maintenance.require_compactable(version.followed, version.staged.table_location, version.so)
    _refuse_a_compaction_others_read_through(version)


def _refuse_rows_not_carried(version: _Judged, *, replaced: set[int], written: set[int]) -> None:
    """The fragments a rewrite writes hold the live rows of those it replaces: by stable row id, else by count.

    A compaction keeps a row's stable row id, so ids compare the rows themselves; the values under an unchanged id are
    not compared. A table without stable row ids can only be held to its count.
    """
    if not version.proposed.has_stable_row_ids:
        live = sum(fragment.count_rows() for fragment in version.followed.get_fragments() if fragment.fragment_id in replaced)
        carried = sum(fragment.count_rows() for fragment in version.proposed.get_fragments() if fragment.fragment_id in written)
        if live != carried:
            raise InvalidInputError(f"commit refused: a Rewrite carries exactly the {live} live rows of the fragments it replaces, not {carried}")
        return
    before, after = Counter(_row_ids(version.followed, replaced)), Counter(_row_ids(version.proposed, written))
    if before == after:
        return
    lost, extra = sorted((before - after).elements()), sorted((after - before).elements())
    raise InvalidInputError(
        f"commit refused: a Rewrite carries exactly the live rows of the fragments it replaces; it loses {len(lost)} "
        f"(stable row ids {lost[:5]}) and adds or repeats {len(extra)} (stable row ids {extra[:5]})"
    )


def _refuse_a_compaction_others_read_through(version: _Judged) -> None:
    """A compaction of a table another dataset resolves its files through is refused, by the maintenance sweep's clone list.

    Judged at both of a compaction's commits, so a refused one leaves the table at its version. Only the referring
    manifests name a table's referrers, so no check of the table finds them; the judge reads the list the sweep's
    whole-estate pre-pass wrote (ADR 0162) and applies its rule (:func:`base_refs.rewrite_refusal`). A list that is
    missing or older than its maximum age refuses, as a protective record read as absent always does here.

    Raises:
        ServiceUnavailableError: No sweep has written a clone list within its maximum age.
        ReferredSourceError: Another dataset resolves its files through this table; the door names it to whoever may see it.
    """
    evidence = version.clone_evidence
    if evidence is None:
        raise ServiceUnavailableError("compaction refused: the maintenance sweep has written no clone list yet, so this table's referrers are unknown")
    clones = evidence.clones
    age = datetime.now(UTC) - clones.taken_at
    if age > evidence.max_age:
        raise ServiceUnavailableError(
            f"compaction refused: the maintenance sweep's clone list is {int(age.total_seconds())}s old, past "
            f"{int(evidence.max_age.total_seconds())}s, so a dataset cloned since is unknown"
        )
    location = version.staged.location
    reason = base_refs.rewrite_refusal(clones.refs(), location, version.followed.tags.list())
    if reason is not None:
        raise maintenance.ReferredSourceError(f"compaction refused: {reason}.", referrers=clones.clones_of(location))


#: What one operation may change beyond the rules every commit meets: a ``ReserveFragments`` and a ``CreateIndex``
#: carry no data, and a ``Rewrite`` carries exactly the rows it replaces. Judged once the version's files are sound.
_OPERATION_RULES: dict[str, Callable[[_Judged], None]] = {
    "ReserveFragments": _refuse_a_reservation_beyond_the_table,
    "CreateIndex": _refuse_changed_data,
    "Rewrite": _refuse_a_rewrite_that_changes_rows,
}


def _refuse_beyond_the_operation(version: _Judged) -> None:
    rule = _OPERATION_RULES.get(version.staged.operation)
    if version.current is None or rule is None:
        return
    rule(version)


class _AddedIndex(BaseModel):
    """One entry of the version's index section that the version it follows does not hold."""

    uuid: str
    name: str
    type: str = ""
    base_id: int | None = None
    fragment_ids: set[int]


#: Index types pylance reads no statistics for: ``index_stats`` on one panics (``lance-index/src/scalar/json.rs``).
_INDEX_TYPES_WITHOUT_STATS = frozenset({"json"})


def _added_indices(version: _Judged) -> list[_AddedIndex]:
    held = _index_uuids(version.current) if version.current is not None else set()
    return [
        _AddedIndex(
            uuid=str(index["uuid"]),
            name=str(index["name"]),
            type=str(index.get("type") or ""),
            base_id=index.get("base_id"),
            fragment_ids=set(index["fragment_ids"]),
        )
        for index in version.proposed.list_indices()
        if str(index["uuid"]) not in held
    ]


def _declared_index_files(version: _Judged) -> dict[str, dict[str, int]]:
    """The files each index the staged transaction adds says it has, by name and size."""
    transaction = version.proposed.read_transaction(version.staged.detached_id)
    new_indices = getattr(transaction.operation, "new_indices", None) if transaction is not None else None
    return {index.uuid: {file.path: file.size_bytes for file in index.files or []} for index in new_indices or []}


def _index_directory(version: _Judged, uuid: str) -> dict[str, int]:
    """The files under the table's own ``_indices/<uuid>/``, by name and size."""
    fs, base = dataplane.dataset_fs(version.staged.location, version.so)
    selector = pafs.FileSelector(f"{base.rstrip('/')}/_indices/{uuid}", allow_not_found=True)
    return {info.path.rsplit("/", 1)[-1]: info.size or 0 for info in fs.get_file_info(selector) if info.is_file}


def _refuse_an_index_outside_its_directory(version: _Judged) -> None:
    """Every index the version adds is read from the table's own ``_indices/<uuid>/``, holds the files it names, and opens.

    Measured on pylance 13.0.0, Lance commits an index naming a base the table does not have, and one whose directory
    holds nothing, and every query the index serves then fails.
    """
    added = _added_indices(version)
    if not added:
        return
    declared = _declared_index_files(version)
    fragments = {fragment.fragment_id for fragment in version.proposed.get_fragments()}
    for index in added:
        _refuse_an_index_read_elsewhere(index, fragments=fragments)
        _refuse_an_index_without_its_files(index, declared=declared.get(index.uuid, {}), present=_index_directory(version, index.uuid))
        _refuse_an_index_that_does_not_open(version.proposed, index)


def _refuse_an_index_read_elsewhere(index: _AddedIndex, *, fragments: set[int]) -> None:
    if index.base_id is not None:
        raise InvalidInputError(
            f"commit refused: index {index.name!r} is read through base {index.base_id}; an index lives under the table's own _indices/{index.uuid}/"
        )
    uncovered = sorted(index.fragment_ids - fragments)
    if uncovered:
        raise InvalidInputError(f"commit refused: index {index.name!r} covers fragment(s) {uncovered[:5]} the version does not hold")


def _refuse_an_index_without_its_files(index: _AddedIndex, *, declared: dict[str, int], present: dict[str, int]) -> None:
    missing = sorted(path for path, size in declared.items() if present.get(path) != size)
    if missing or not present:
        raise InvalidInputError(
            f"commit refused: _indices/{index.uuid}/ does not hold index {index.name!r}'s file(s) {missing[:5] or '(none present)'} at their sizes"
        )


def _refuse_an_index_that_does_not_open(proposed: lance.LanceDataset, index: _AddedIndex) -> None:
    """The index opens: ``index_stats`` reads it. The JSON index type has none, so its directory is all that is judged."""
    if index.type.lower() in _INDEX_TYPES_WITHOUT_STATS:
        return
    try:
        proposed.stats.index_stats(index.name)
    except OSError as exc:
        raise InvalidInputError(f"commit refused: index {index.name!r} does not open: {exc}") from exc


class Verdict(BaseModel):
    """A staged version the judge admitted: whether it adds data, and the files it adds as the store held them."""

    adds_data: bool
    #: Read before any of the version's files is judged, so a file replaced after the read is found later ([[LH-419]]).
    files: file_identities.CommittedFiles


def judge(
    staged: StagedVersion,
    so: StorageOptions,
    *,
    base_judge: BaseJudge,
    external_bases: Sequence[base_registry.RecordedBase],
    clone_evidence: CloneEvidence | None = None,
) -> Verdict:
    """Refuse the staged version if it breaks a rule the catalog's own write doors hold; say whether it adds data, and
    what it adds under the table's root.

    The version is compared with the one it follows. What it adds is judged: a file or a row carried over was judged
    when it landed. It adds data when it names a data file the version it follows does not hold, unless it is a
    compaction or a restore, whose files carry rows the table already held: such a commit on a derived tier names its
    run. A compaction is judged against ``clone_evidence``, which the caller reads for the two operations in
    :data:`COMPACTION_OPERATIONS` and no other.

    Raises:
        InvalidInputError: The version breaks one of the rules below; nothing was finalized.
        ServiceUnavailableError: A compaction arrived with no clone list within its maximum age.
        UnsupportedOperationError: The version needs a Lance feature this catalog does not serve, or compacts a table
            another dataset resolves its files through.
    """
    proposed = _open(staged, so)
    current = None if staged.creates else lance.dataset(staged.location, version=staged.version - 1, storage_options=so.in_clear() or None)
    _refuse_changed_invariants(proposed, current)
    _refuse_changed_schema_governance(proposed, current)
    encoding.require_no_new_invalid_encoding(current, proposed)
    _refuse_config(proposed, current)
    version = _Judged(staged=staged, so=so, proposed=proposed, current=current, split=_split(proposed, current), clone_evidence=clone_evidence)
    files = committed_files.added(
        current,
        proposed,
        table_location=staged.table_location,
        branch=staged.branch,
        so=so,
        transaction_uuid=staged.transaction_uuid,
        read_version=staged.read_version,
    )
    _refuse_bases(version, base_judge)
    _refuse_foreign_file_versions(version)
    _refuse_added_feature_flags(proposed, current)
    _refuse_malformed_new_fragments(version)
    _refuse_rows_coming_back(version)
    _refuse_forged_files(version)
    _refuse_unverified_blobs(version, external_bases)
    primary_keys.refuse_repeats_in_version(proposed, current, door="version/create")
    _refuse_beyond_the_operation(version)
    _refuse_an_index_outside_its_directory(version)
    return Verdict(adds_data=version.adds_data, files=files)


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
    "ReserveFragments": lineage_emit.COMPACT_TABLE,
    "Overwrite": lineage_emit.OVERWRITE_TABLE,
}


def staged_field_labels(staged: StagedVersion, so: StorageOptions) -> dict[str, dict[str, str]]:
    """The ``rask.*`` metadata on the staged version's fields, by dotted path: what a table's first version labels."""
    return provenance_guard.field_labels(_open(staged, so).schema)


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


def finalize(table: table_claims.DoorTable, staged: StagedVersion, request: CreateTableVersionRequest, so: StorageOptions) -> CreateTableVersionResponse:
    """Finalize the judged bytes through the backend's put-if-not-exists; a lost race is the backend's 409.

    The backend is handed the catalog's own copy, which it moves into the version slot (measured on pylance 13.0.0), so
    the version that lands is the one judged whatever the writer puts over its staged key meanwhile. The copy carries the
    writer's transaction byte for byte, and a client whose answer was lost relies on that: Lance reads the version back
    and keeps the commit as its own only when the transaction there is the one it staged ([[LH-491]]). The finalize names
    the table and no location, so it is the door's guarded native write ([[LH-530]]).
    """
    path = f"{dataplane.object_store_path(staged.location.rstrip('/'))}/_versions/d{staged.finalize_id}.manifest"
    judged = request.model_copy(update={"manifest_path": path})
    try:
        response = CreateTableVersionResponse.model_validate(table.write_natively("create_table_version", judged))
    except BaseException:
        refuse(staged, so)
        raise
    _delete_staged(staged, so)
    return response


def _delete_staged(staged: StagedVersion, so: StorageOptions) -> None:
    """Delete the writer's staged manifest once its version landed."""
    dataplane.delete_table_object(staged.location, so, f"_versions/{staged.staged_uri.rsplit('/', 1)[-1]}")
