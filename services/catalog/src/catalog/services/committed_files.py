"""The files a version adds under its table's root, as the store holds them when the catalog commits it ([[LH-419]]).

Every catalog commit records them before its version is announced (``lakehouse.file_identities``), and
maintenance's sweep reads each one again: ``version/create`` reads them before it judges a stock commit's files, a door's
stamped commit (``dataplane.commit_stamped``) before its finalize, and a write Lance commits in one call (the create
door's ``write_dataset``, an unkeyed ``/insert``) just after it lands (:func:`record_landed`).

What a version adds against the version it follows on the same ref, each a file a writer's vend may stage
(``table_layout.WRITER_PUT_GLOBS``) and, once sealed here, never put over (ADR 0242): the data file of each fragment it holds anew (``data/*.lance``,
``lance_docs/lance/format/table/layout.md`` § Basic Layout), the blob sidecars beside it when the table has a blob column
(``blob.md`` § Sidecar Resolution), each deletion file a fragment did not carry (``_deletions/``), the files of each index
it adds (``_indices/<uuid>/``), and the transaction file its manifest names (``_transactions/<read version>-<uuid>.txn``,
``layout.md`` § Transaction Files). pylance 13.0.0's ``read_transaction`` reads the manifest's inline copy when it carries
one (measured: a zeroed ``.txn`` read back unchanged) and the file when it carries none, so the file is sealed like any
other. A version naming a file the store does not hold is refused (:class:`lakehouse.file_identities.FilesNotHeldError`):
the key would stay a writer's to put later, unsealed and unrecorded. A file named through a base is not under the
table's root, and no vend puts there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Final

import lance
import pyarrow.fs as pafs
from lance.dataset import Transaction
from lance_namespace import InvalidInputError, ServiceUnavailableError
from opentelemetry import metrics

from catalog.core.config import get_settings
from catalog.services import client_fragments
from lakehouse import base_refs, file_identities, manifest_wire, stored_objects
from lakehouse.branch_layout import BRANCH_CONTAINER
from lakehouse.objectfs import fs_and_base
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: Commits refused before their version landed because the store refused, or did not answer, a seal (ADR 0242 B4), by
#: ``lance.seal_failure`` (``refused``, ``unavailable``).
_seal_failed = metrics.get_meter("lance.catalog").create_counter(
    "catalog.commit.seal_failed",
    unit="{commit}",
    description="Commits refused before their version landed because the object store refused, or did not answer, sealing a file they add.",
)

#: Commits Lance made itself whose files the catalog could not record, so the sweep never reads them ([[LH-419]]).
#: Added to at every such commit, zero included, so the series exists before the first miss and the alert's
#: ``increase()`` sees that miss as a step (``CommittedFilesUnrecorded``).
_unrecorded = metrics.get_meter("lance.catalog").create_counter(
    "catalog.committed_files.unrecorded",
    unit="{commit}",
    description="Commits a write door landed whose added files the catalog could not record, so maintenance never checks them.",
)


def added(
    before: lance.LanceDataset | None,
    after: lance.LanceDataset,
    *,
    table_location: str,
    branch: str | None,
    so: StorageOptions,
    transaction_uuid: str,
    read_version: int,
    seal_transaction: bool = True,
) -> file_identities.CommittedFiles:
    """What ``after`` adds over ``before`` (``None`` for a table's first version) on ``branch`` (``None`` for main), the
    version whose transaction ``transaction_uuid`` was planned on ``read_version``, each file sealed and every one the
    version names held (ADR 0242). Without ``seal_transaction`` the transaction file is required and recorded and left for
    the caller to seal once the version lands (:func:`seal_landed_transaction`): a stock writer's, which it puts again
    under the same name on each attempt of the commit.

    Raises:
        FilesNotHeldError: The version names a file the store does not hold.
        InvalidInputError: The version's manifest names a transaction file other than its transaction's own.
        ServiceUnavailableError: The store refused, or did not answer, a seal on a warehouse whose store must enforce it:
            the commit has not landed, and a retry finds the store answering or the operator's grant in place.
        OSError: The store refused, or could not be reached for, a read.
    """
    root = table_root(table_location)
    paths = added_paths(before, after, root=root, branch=branch, so=so)
    planned = transaction_file(branch, read_version, transaction_uuid)
    if (named := _named_transaction(after, branch)) is not None and named != planned:
        raise InvalidInputError(f"commit refused: the manifest names transaction file {named!r}, and its transaction's own is {planned!r}")
    unsealed = [named] if named is not None and not seal_transaction else []
    try:
        return file_identities.as_stored(root, so, transaction_uuid, paths, where_granted=detection_only(table_location), unsealed=unsealed)
    except stored_objects.SealRefusedError as exc:
        _count_seal_failed(table_location, "refused", exc)
        raise ServiceUnavailableError(
            "the commit was not landed: the object store refused sealing a file it adds (s3:PutObjectTagging), which every "
            "warehouse not declared detection-only requires; the store's operator grants it, then retry"
        ) from exc
    except stored_objects.SealUnavailableError as exc:
        _count_seal_failed(table_location, "unavailable", exc)
        raise ServiceUnavailableError("the commit was not landed: the object store did not answer sealing a file it adds; retry") from exc


def seal_landed_transaction(table_location: str, so: StorageOptions, committed: file_identities.CommittedFiles) -> None:
    """Seal the transaction file of a stock commit whose version has landed, which :func:`added` left unsealed: the writer
    puts it no more. A seal the store refuses or does not answer is logged at ERROR and counted, and the version stays
    landed: no reader of it reads the file, since ``version/create`` admits only a manifest carrying its transaction
    inline, and the commit's record keeps the file's identity, which maintenance's pass reads again."""
    paths = [file.path for file in committed.files if f"/{TRANSACTIONS}/" in f"/{file.path}"]
    try:
        stored_objects.seal(table_root(table_location), so, paths, where_granted=detection_only(table_location))
    except (stored_objects.SealRefusedError, stored_objects.SealUnavailableError) as exc:
        _count_seal_failed(table_location, "after_landing", exc)


#: The directory of a ref's transaction files (``lance_docs/lance/format/table/layout.md`` § Transaction Files).
TRANSACTIONS: Final = "_transactions"


def found(
    before: lance.LanceDataset | None, after: lance.LanceDataset, *, table_location: str, branch: str | None, so: StorageOptions, transaction_uuid: str
) -> file_identities.CommittedFiles:
    """What ``after`` adds over ``before`` as the store holds it, sealing nothing: the files of an attempt that never
    lands, read to delete them ([[LH-564]]).

    Raises:
        OSError: The store refused, or could not be reached for, a read.
    """
    root = table_root(table_location)
    return file_identities.as_found(root, so, transaction_uuid, added_paths(before, after, root=root, branch=branch, so=so))


def added_paths(before: lance.LanceDataset | None, after: lance.LanceDataset, *, root: str, branch: str | None, so: StorageOptions) -> list[str]:
    """Every file under ``root`` that ``after`` names and ``before`` does not, relative to ``root``.

    Named by the version, each of which the store must hold: each new data file, each new deletion file, and the
    transaction file its manifest names. Listed, as the store holds them: each added index's directory and the sidecars
    beside a new data file of a blob column, whose completeness the index and blob rules judge (a file an index declares,
    and a blob a data file names, are refused absent). The other files a version can reference under its root are of
    features no commit may turn on (``lakehouse.features.SUPPORTED``): an overlay's data file (flag 64), a spilled row
    lineage column (2048), a fragment tree's objects under ``_bt/`` (4096), managed blobs under ``_blobs/`` (16384), and a
    MemWAL, which ``version/create`` refuses by its index.
    """
    ref = _ref_prefix(branch)
    data = list(_data_files(before, after))
    listed = [f"{ref}data/{name.removesuffix('.lance')}" for name in data] if client_fragments.blob_columns(after) else []
    listed += [f"{ref}_indices/{uuid}" for uuid in _indices(before, after)]
    named = _named_transaction(after, branch)
    return [
        *(f"{ref}data/{name}" for name in data),
        *(f"{ref}{path}" for path in _deletion_files(before, after)),
        *_listed(root, so, listed),
        *([named] if named is not None else []),
    ]


#: ``string transaction_file = 12`` of ``message Manifest``: the name, under the ref's ``_transactions/``, of the file
#: holding the version's transaction, empty for none. Measured on pylance 13.0.0: ``<read version>-<uuid>.txn`` on every
#: version it writes, and Lance reads the version's transaction from it whenever the manifest carries none inline
#: (``lance_docs/lance/format/table/transaction.md`` § Inline Transaction Sections).
TRANSACTION_FILE_FIELD: Final = 12


def transaction_file_in(message: bytes) -> str | None:
    """The transaction file a ``Manifest`` protobuf ``message`` names, ``None`` when it names none.

    Raises:
        InvalidInputError: The name is not one file directly under ``_transactions/``.
        manifest_wire.ManifestWireError: ``message`` does not parse.
    """
    value = next((value for number, value in manifest_wire.fields(message) if number == TRANSACTION_FILE_FIELD and isinstance(value, bytes)), None)
    if not value:
        return None
    name = value.decode("utf-8", errors="replace")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise InvalidInputError(f"commit refused: the manifest names transaction file {name!r}, which is not one file under _transactions/")
    return name


def _named_transaction(dataset: lance.LanceDataset, branch: str | None) -> str | None:
    manifest: bytes = dataset._ds.serialized_manifest()  # noqa: SLF001 — pylance exposes no manifest field; its own __reduce__ path reads this
    name = transaction_file_in(manifest)
    return None if name is None else f"{_ref_prefix(branch)}_transactions/{name}"


def detection_only(table_location: str) -> bool:
    """Whether git declares the warehouse whose bucket holds ``table_location`` runs with detection only
    (``catalog.declared.warehouses[].seal``, ``LANCE_SEAL_DETECTION_ONLY_BUCKETS``, ADR 0242 § The declared exception): its
    commits tag where the store grants it and go on where it refuses. Any other warehouse's commit fails rather than land
    a file unsealed."""
    if not table_location.startswith("s3://"):
        return False
    return table_location.removeprefix("s3://").partition("/")[0] in get_settings().seal_detection_only_buckets


def _count_seal_failed(table_location: str, reason: str, exc: OSError) -> None:
    _seal_failed.add(1, {"lance.seal_failure": reason})
    log.error("commit_seal_failed", extra={"location": table_location, "reason": reason, "error": str(exc)[:300]})


def table_root(table_location: str) -> str:
    """The root of the table at ``table_location`` as its store lists it: the key the location decodes to, under the
    store's scheme. Every path here is relative to it."""
    return base_refs.key_uri(base_refs.store_of(table_location), base_refs.decoded_path(table_location))


def transaction_file(branch: str | None, read_version: int, transaction_uuid: str) -> str:
    """The transaction file a commit of ``branch`` (``None`` for main) planned on ``read_version`` writes
    (``lance_docs/lance/format/table/layout.md`` § Transaction Files)."""
    return f"{_ref_prefix(branch)}_transactions/{read_version}-{transaction_uuid}.txn"


#: The Lance operations a commit writes its own new files for (pylance 13.0.0: ``add_columns`` plans a ``Merge``). A
#: ``Restore`` names an older version's files as new to the version it follows, and that version still holds them.
_WRITES_ITS_OWN_FILES: Final = (
    lance.LanceOperation.Append,
    lance.LanceOperation.DataReplacement,
    lance.LanceOperation.Delete,
    lance.LanceOperation.Merge,
    lance.LanceOperation.Overwrite,
    lance.LanceOperation.Update,
)


def written_by(transaction: Transaction, committed: file_identities.CommittedFiles | None, *, branch: str | None) -> list[str]:
    """The files a commit of ``transaction`` that never landed wrote itself ([[LH-564]]): its transaction file, and every
    file ``committed`` names when its operation writes its own new files."""
    own = [transaction_file(branch, transaction.read_version, transaction.uuid)]
    if committed is not None and isinstance(transaction.operation, _WRITES_ITS_OWN_FILES):
        own += [file.path for file in committed.files if file.path not in own]
    return own


def _ref_prefix(branch: str | None) -> str:
    return "" if branch is None else f"{BRANCH_CONTAINER}/{branch}/"


def record_landed(
    record: Callable[[file_identities.CommittedFiles], None], after: lance.LanceDataset, *, table_location: str, branch: str | None, so: StorageOptions
) -> Transaction | None:
    """Record, through ``record``, what ``after`` added over the version before it: a version Lance committed itself.
    Answers the version's transaction, which its author is recorded under (:mod:`catalog.services.commit_authors`), or
    ``None`` when none could be read.

    After the version lands, since the write and its commit are one call: a store failure here is logged at ERROR and
    counted, and the door still answers its version, because the write landed and an error would have its caller retry
    it. So is a version that names no transaction, which no record can be keyed by.
    """
    try:
        transaction = after.read_transaction(after.version)
    except OSError as exc:
        _count_unrecorded(table_location, after.version, f"{type(exc).__name__}: {exc}")
        return None
    if transaction is None:
        _count_unrecorded(table_location, after.version, "the version names no transaction")
        return None
    try:
        before = None if branch is None and after.version == 1 else after.checkout_version(after.version - 1 if branch is None else (branch, after.version - 1))
        record(
            added(before, after, table_location=table_location, branch=branch, so=so, transaction_uuid=transaction.uuid, read_version=transaction.read_version)
        )
    except (OSError, file_identities.FilesNotHeldError, InvalidInputError, ServiceUnavailableError) as exc:
        _count_unrecorded(table_location, after.version, f"{type(exc).__name__}: {exc}")
        return transaction
    _unrecorded.add(0)
    return transaction


def _count_unrecorded(table_location: str, version: int, reason: str) -> None:
    _unrecorded.add(1)
    log.error("committed_files_unrecorded", extra={"location": table_location, "version": version, "error": reason[:300]})


def _data_files(before: lance.LanceDataset | None, after: lance.LanceDataset) -> Iterator[str]:
    held = {data_file.path for fragment in before.get_fragments() for data_file in fragment.data_files()} if before is not None else set()
    for fragment in after.get_fragments():
        for data_file in fragment.data_files():
            if data_file.base_id is None and data_file.path not in held:
                yield data_file.path


def _deletion_files(before: lance.LanceDataset | None, after: lance.LanceDataset) -> Iterator[str]:
    held = {_deletion_path(fragment) for fragment in before.get_fragments()} if before is not None else set()
    for fragment in after.get_fragments():
        if (path := _deletion_path(fragment)) is not None and path not in held:
            yield path


def _deletion_path(fragment: lance.LanceFragment) -> str | None:
    deletion = fragment.metadata.deletion_file
    if deletion is None or deletion.base_id is not None:
        return None
    return deletion.path(fragment.fragment_id)


def _indices(before: lance.LanceDataset | None, after: lance.LanceDataset) -> Iterator[str]:
    held = {str(index["uuid"]) for index in before.list_indices()} if before is not None else set()
    for index in after.list_indices():
        if str(index["uuid"]) not in held and index.get("base_id") is None:
            yield str(index["uuid"])


def _listed(root: str, so: StorageOptions, directories: list[str]) -> Iterator[str]:
    """Every file under each of ``directories``, relative to ``root``."""
    if not directories:
        return
    fs, base = fs_and_base(root, so)
    for directory in directories:
        for info in fs.get_file_info(pafs.FileSelector(f"{base}/{directory}", allow_not_found=True, recursive=True)):
            if info.type == pafs.FileType.File:
                yield info.path.removeprefix(f"{base}/")
