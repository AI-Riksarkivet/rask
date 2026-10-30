"""The files a version adds under its table's root, as the store holds them when the catalog commits it ([[LH-419]]).

Every catalog commit records them before its version is announced (``service_kit.lakehouse.file_identities``), and
maintenance's sweep reads each one again: ``version/create`` reads them before it judges a stock commit's files, a door's
stamped commit (``dataplane.commit_stamped``) before its finalize, and a write Lance commits in one call (the create
door's ``write_dataset``, an unkeyed ``/insert``) just after it lands (:func:`record_landed`).

What a version adds against the version it follows on the same ref, each a file a writer's vend may put over
(``table_layout.WRITER_PUT_GLOBS``): the data file of each fragment it holds anew (``data/*.lance``,
``lance_docs/lance/format/table/layout.md`` § Basic Layout), the blob sidecars beside it when the table has a blob column
(``blob.md`` § Sidecar Resolution), each deletion file a fragment did not carry (``_deletions/``), the files of each index
it adds (``_indices/<uuid>/``), and its own transaction file (``_transactions/<read version>-<uuid>.txn``, ``layout.md`` §
Transaction Files). pylance 13.0.0's ``read_transaction`` reads the manifest's inline copy (measured: a zeroed ``.txn``
read back unchanged), and the file is still one the version names and a writer may replace. A file named through a base
is not under the table's root, and no vend puts there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import lance
import pyarrow.fs as pafs
from opentelemetry import metrics

from catalog.services import client_fragments
from service_kit.lakehouse import base_refs, file_identities
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

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
) -> file_identities.CommittedFiles:
    """What ``after`` adds over ``before`` (``None`` for a table's first version) on ``branch`` (``None`` for main), the
    version whose transaction ``transaction_uuid`` was planned on ``read_version``.

    Raises:
        OSError: The store refused, or could not be reached for, a read.
    """
    root = base_refs.key_uri(base_refs.store_of(table_location), base_refs.decoded_path(table_location))
    ref = "" if branch is None else f"{BRANCH_CONTAINER}/{branch}/"
    data = list(_data_files(before, after))
    listed = [f"{ref}data/{name.removesuffix('.lance')}" for name in data] if client_fragments.blob_columns(after) else []
    listed += [f"{ref}_indices/{uuid}" for uuid in _indices(before, after)]
    paths = [
        *(f"{ref}data/{name}" for name in data),
        *(f"{ref}{path}" for path in _deletion_files(before, after)),
        *_listed(root, so, listed),
        f"{ref}_transactions/{read_version}-{transaction_uuid}.txn",
    ]
    return file_identities.as_stored(root, so, transaction_uuid, paths)


def record_landed(
    record: Callable[[file_identities.CommittedFiles], None], after: lance.LanceDataset, *, table_location: str, branch: str | None, so: StorageOptions
) -> None:
    """Record, through ``record``, what ``after`` added over the version before it: a version Lance committed itself.

    After the version lands, since the write and its commit are one call: a store failure here is logged at ERROR and
    counted, and the door still answers its version, because the write landed and an error would have its caller retry
    it. So is a version that names no transaction, which no record can be keyed by.
    """
    transaction = after.read_transaction(after.version)
    if transaction is None:
        _count_unrecorded(table_location, after.version, "the version names no transaction")
        return
    try:
        before = None if branch is None and after.version == 1 else after.checkout_version(after.version - 1 if branch is None else (branch, after.version - 1))
        record(
            added(before, after, table_location=table_location, branch=branch, so=so, transaction_uuid=transaction.uuid, read_version=transaction.read_version)
        )
    except OSError as exc:
        _count_unrecorded(table_location, after.version, f"{type(exc).__name__}: {exc}")
        return
    _unrecorded.add(0)


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
