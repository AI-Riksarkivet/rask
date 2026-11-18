"""The files a version adds under its table's root, sealed and recorded as the store holds them ([[LH-419]], ADR 0242).

ONE FUNCTION SEALS AND RECORDS (:func:`seal_and_record`), whatever path adds the files: it reads the warehouse's
posture git declares (:func:`detection_only`), seals each file (``lakehouse.stored_objects.seal``), refuses a file the
store does not hold, reads the identity it records, and records it; a refusal or a store that does not answer fails a
commit that has not landed with the retryable 503, counted on ``catalog.commit.seal_failed``, and one that has landed is
counted on ``catalog.committed_files.unrecorded`` and left to the reconciler. The function that seals and reads
(:func:`_as_stored`) is this module's own, so no path seals a commit's files past the posture, the mapping or the count.

WHEN EACH PATH SEALS, measured against what each path writes before its version lands (ADR 0242 § The order of effects):

- ``version/create`` (a stock writer): every file the version names is sealed and read at the judgement, before the
  finalize, and recorded once the commit is authorized; the writer's transaction file is required there and sealed and
  recorded only once the version landed (:func:`seal_and_record_transaction`), since a stock writer that loses a race
  puts it again under the same name.
- a door's stamped commit (``dataplane.commit_stamped``): every file, its transaction file included, before the
  finalize (:func:`added`).
- a write Lance commits itself (``dataplane._land_rows`` for ``/insert``, ``dataplane._write_blob`` for ``/create``):
  its fragments are written first, and their files sealed and recorded before the commit (:func:`appended`); the
  transaction file Lance writes inside its commit, once the version landed (:func:`seal_and_record_transaction`).
- a landed version whose files no commit sealed and recorded (its writer died between its landing and its last seal, or a
  version made with no seal of its own: a branch's first, a disarm) is finished by the commit reconciler
  (:func:`finish_landed`), counted on ``catalog.committed_files.finished``.

What a version adds against the version it follows on the same ref, each a file a writer's vend may stage
(``table_layout.WRITER_PUT_GLOBS``) and, once sealed here, never put over: the data file of each fragment it holds anew (``data/*.lance``,
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
from collections.abc import Callable, Iterator, Sequence
from typing import Final

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from botocore.exceptions import BotoCoreError, ClientError
from lance.dataset import Transaction
from lance_namespace import InternalError, InvalidInputError, LanceNamespaceError, ServiceUnavailableError
from opentelemetry import metrics

from catalog.core.config import get_settings
from catalog.services import client_fragments
from lakehouse import base_refs, blobs, file_identities, manifest_wire, stored_objects
from lakehouse.branch_layout import BRANCH_CONTAINER
from lakehouse.objectfs import fs_and_base
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")

#: Commits whose seal the store refused or did not answer (ADR 0242 B4), by ``lance.seal_failure``: ``refused`` and
#: ``unavailable`` were refused before their version landed (503); ``after_landing`` had landed, and its file waits for the
#: reconciler.
_seal_failed = _meter.create_counter(
    "catalog.commit.seal_failed",
    unit="{commit}",
    description="Commits whose seal of a file they add the object store refused or did not answer: before their version landed (refused 503), or after.",
)

#: Landed versions some of whose files the catalog could not seal and record when they landed, so maintenance's sweep
#: does not read them until the reconciler finishes them ([[LH-419]]). Added to at every landing, zero included, so the
#: series exists before the first miss and the alert's ``increase()`` sees that miss as a step (``CommittedFilesUnrecorded``).
_unrecorded = _meter.create_counter(
    "catalog.committed_files.unrecorded",
    unit="{commit}",
    description="Landed versions some of whose files the catalog could not seal and record as they landed, so maintenance does not check them until the reconciler finishes them.",
)

#: Landed versions whose files the commit reconciler sealed and recorded because no commit had (:func:`finish_landed`).
_finished = _meter.create_counter(
    "catalog.committed_files.finished",
    unit="{commit}",
    description="Landed versions whose files the commit reconciler sealed and recorded because no commit had: one whose writer died between "
    "its landing and its last seal, or one made with no seal of its own (a branch's first version, a disarm).",
)

#: Where a commit's files are recorded: the table's own record of them (``DoorTable.record_committed_files``).
type Recorder = Callable[[file_identities.CommittedFiles], None]


def create_series() -> None:
    """Create each counter's series at 0 when the catalog starts, so its first count is an increase an alert can read."""
    for counter in (_unrecorded, _finished):
        counter.add(0)


def seal_and_record(
    paths: Sequence[str],
    *,
    table_location: str,
    so: StorageOptions,
    transaction_uuid: str,
    record: Recorder | None,
    landed: int | None = None,
    unsealed: Sequence[str] = (),
    adding_to: file_identities.CommittedFiles | None = None,
) -> file_identities.CommittedFiles | None:
    """Seal each file at ``paths`` under the table's root, read the identity the store holds for it, and record them,
    with what ``adding_to`` already holds, through ``record``: the one call every path adding files to a version makes.

    ``landed`` is the version, once it landed: a failure then fails nothing, since the write landed and an error would
    have its caller write it again; it is logged at ERROR, counted (``catalog.committed_files.unrecorded``) and left for
    the reconciler, and ``None`` is answered. Before the version lands, every failure raises. ``record`` is ``None`` for
    ``version/create``, which records the files it is answered only once the commit is authorized. ``unsealed`` are paths
    among ``paths`` the store must hold and that are left out of the record, for the caller to seal and record once the
    version landed: a stock writer's transaction file, which it puts again under the same name on each attempt of one
    commit (measured on pylance 13.0.0).

    Raises:
        FilesNotHeldError: The store holds no object at one of ``paths``.
        ServiceUnavailableError: The store refused, or did not answer, a seal on a warehouse whose store must enforce it:
            the commit has not landed, and a retry finds the store answering or the operator's grant in place.
        OSError: The store refused, or could not be reached for, a read or the record.
    """
    try:
        sealed = _as_stored(table_root(table_location), so, transaction_uuid, paths, where_granted=detection_only(table_location), unsealed=unsealed)
        held = [] if adding_to is None else [file for file in adding_to.files if file.path not in {added.path for added in sealed.files}]
        committed = sealed.model_copy(update={"files": [*held, *sealed.files]})
        if record is not None:
            record(committed)
    except (stored_objects.SealRefusedError, stored_objects.SealUnavailableError) as exc:
        refused = isinstance(exc, stored_objects.SealRefusedError)
        _count_seal_failed(table_location, "after_landing" if landed is not None else "refused" if refused else "unavailable", exc)
        if landed is not None:
            _count_unrecorded(table_location, landed, f"{type(exc).__name__}: {exc}")
            return None
        if refused:
            raise ServiceUnavailableError(
                "the commit was not landed: the object store refused sealing a file it adds (s3:PutObjectTagging), which every "
                "warehouse not declared detection-only requires; the store's operator grants it, then retry"
            ) from exc
        raise ServiceUnavailableError("the commit was not landed: the object store did not answer sealing a file it adds; retry") from exc
    except (OSError, file_identities.FilesNotHeldError, LanceNamespaceError, BotoCoreError, ClientError) as exc:
        if landed is None:
            raise
        _count_unrecorded(table_location, landed, f"{type(exc).__name__}: {exc}")
        return None
    if landed is not None:
        _unrecorded.add(0)
    return committed


def _as_stored(
    table_root: str, storage_options: StorageOptions, transaction_uuid: str, paths: Sequence[str], *, where_granted: bool, unsealed: Sequence[str]
) -> file_identities.CommittedFiles:
    """The files at ``paths`` under ``table_root`` (the store's own spelling of it), each sealed and then read as the
    store holds it, less ``unsealed``, which are only required held.

    THE SEAL COMES FIRST, so the bytes read are the bytes no put can replace: from the seal on, the store refuses every
    session of the catalog's identity a put over the file (:func:`stored_objects.seal`), and the identity recorded is the
    one the store keeps. EVERY PATH MUST BE HELD: a commit naming a file the store does not hold would leave a key a writer
    could put afterwards, untagged and unrecorded, under a committed version.

    Raises:
        FilesNotHeldError: The store holds no object at one of ``paths``.
        stored_objects.SealRefusedError: The store refused a seal, and ``where_granted`` is off.
        stored_objects.SealUnavailableError: The store did not answer a seal.
        OSError: The store refused or could not be reached for a read.
    """
    absent = set(stored_objects.seal(table_root, storage_options, [path for path in paths if path not in unsealed], where_granted=where_granted))
    found = stored_objects.identities(table_root, storage_options, paths)
    absent |= {path for path in paths if path not in found}
    if absent:
        raise file_identities.FilesNotHeldError(sorted(absent))
    return file_identities.CommittedFiles(
        transaction_uuid=transaction_uuid, files=[file_identities.CommittedFile(path=path, identity=found[path]) for path in paths if path not in unsealed]
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
    record: Recorder | None,
    seal_transaction: bool = True,
) -> file_identities.CommittedFiles:
    """What ``after`` adds over ``before`` (``None`` for a table's first version) on ``branch`` (``None`` for main), the
    version whose transaction ``transaction_uuid`` was planned on ``read_version``, sealed and recorded through ``record``
    before it lands (:func:`seal_and_record`), every file the version names held. Without ``seal_transaction`` the
    transaction file is required and left for the caller to seal and record once the version lands
    (:func:`seal_and_record_transaction`): a stock writer's, which it puts again under the same name on each attempt.

    Raises:
        FilesNotHeldError: The version names a file the store does not hold.
        InvalidInputError: The version's manifest names a transaction file other than its transaction's own.
        ServiceUnavailableError: The store refused, or did not answer, a seal on a warehouse whose store must enforce it.
        OSError: The store refused, or could not be reached for, a read.
    """
    paths = added_paths(before, after, root=table_root(table_location), branch=branch, so=so)
    planned = transaction_file(branch, read_version, transaction_uuid)
    if (named := _named_transaction(after, branch)) is not None and named != planned:
        raise InvalidInputError(f"commit refused: the manifest names transaction file {named!r}, and its transaction's own is {planned!r}")
    unsealed = [named] if named is not None and not seal_transaction else []
    committed = seal_and_record(paths, table_location=table_location, so=so, transaction_uuid=transaction_uuid, record=record, unsealed=unsealed)
    if committed is None:
        raise InternalError("the files of a commit that has not landed were answered as landed")
    return committed


def seal_and_record_transaction(
    record: Recorder, committed: file_identities.CommittedFiles, *, table_location: str, branch: str | None, so: StorageOptions, read_version: int, version: int
) -> None:
    """Seal and record, with ``committed``, the transaction file of the commit that landed as ``version``: the one file a
    commit cannot seal before its version lands, since Lance writes it inside its own commit and a stock writer puts its
    own again on each attempt. A failure is counted and left for the reconciler (:func:`seal_and_record`, ``landed``)."""
    seal_and_record(
        [transaction_file(branch, read_version, committed.transaction_uuid)],
        table_location=table_location,
        so=so,
        transaction_uuid=committed.transaction_uuid,
        record=record,
        landed=version,
        adding_to=committed,
    )


def finish_landed(
    record: Recorder,
    recorded: Callable[[str], file_identities.CommittedFiles | None],
    before: lance.LanceDataset | None,
    after: lance.LanceDataset,
    *,
    table_location: str,
    branch: str | None,
    so: StorageOptions,
    transaction_uuid: str,
) -> bool:
    """Seal and record what the landed version ``after`` names over ``before`` and its commit's record (``recorded``, by
    transaction uuid) lacks: what a writer that died between its landing and its last seal left, and the files of a version
    made with no seal of its own. Whether it finished anything; counted on ``catalog.committed_files.finished``. Run by the
    commit reconciler on every version it reads (``commit_reconcile.scan_ref``), on its period. A failure is counted as
    :func:`seal_and_record` counts one after landing, and the next pass tries again.
    """
    try:
        paths = added_paths(before, after, root=table_root(table_location), branch=branch, so=so)
        held = recorded(transaction_uuid)
    except (OSError, ValueError, BotoCoreError, ClientError) as exc:
        _count_unrecorded(table_location, int(after.version), f"{type(exc).__name__}: {exc}")
        return False
    missing = [path for path in paths if held is None or path not in {file.path for file in held.files}]
    if not missing:
        return False
    if (
        seal_and_record(
            missing, table_location=table_location, so=so, transaction_uuid=transaction_uuid, record=record, landed=int(after.version), adding_to=held
        )
        is None
    ):
        return False
    _finished.add(1)
    log.warning("committed_files_finished", extra={"location": table_location, "version": int(after.version), "branch": branch, "files": missing[:20]})
    return True


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


def _count_seal_failed(table_location: str, reason: str, exc: Exception) -> None:
    _seal_failed.add(1, {"lance.seal_failure": reason})
    log.error("commit_seal_failed", extra={"location": table_location, "reason": reason, "error": str(exc)[:300]})


def appended(
    fragments: Sequence[lance.FragmentMetadata],
    schema: pa.Schema,
    *,
    table_location: str,
    branch: str | None,
    so: StorageOptions,
    transaction_uuid: str,
    record: Recorder,
) -> file_identities.CommittedFiles:
    """What ``fragments``, written for a transaction of a table of ``schema`` that has yet to land on its ref (``branch``),
    add under the table's root, sealed and recorded before their version lands (:func:`seal_and_record`): each data file
    and, where the schema holds a blob column, the sidecars beside it. Lance writes the transaction's own file inside its
    commit (pylance 13.0.0), so :func:`seal_and_record_transaction` seals that one once the version names it.

    Raises:
        FilesNotHeldError: The store holds no object at a file the fragments name.
        ServiceUnavailableError: The store refused, or did not answer, a seal on a warehouse whose store must enforce it.
        OSError: The store refused, or could not be reached for, a read or the record.
    """
    root = table_root(table_location)
    ref = _ref_prefix(branch)
    data = [data_file.path for fragment in fragments for data_file in fragment.data_files() if data_file.base_id is None]
    listed = [f"{ref}data/{name.removesuffix('.lance')}" for name in data] if any(blobs.holds_blob(field) for field in schema) else []
    committed = seal_and_record(
        [*(f"{ref}data/{name}" for name in data), *_listed(root, so, listed)],
        table_location=table_location,
        so=so,
        transaction_uuid=transaction_uuid,
        record=record,
    )
    if committed is None:
        raise InternalError("the files of a write that has not landed were answered as landed")
    return committed


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
