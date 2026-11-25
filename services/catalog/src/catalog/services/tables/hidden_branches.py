"""Hide a deleted branch, put it back, or delete it for good ([[LH-429]], :mod:`lakehouse.records.branch_trash`).

A recoverable delete keeps the branch whole: its record is filed first, the catalog tags the version it was cut from,
and only then does the reference file go. A branch is hidden once its record stands and its reference file is gone
(:func:`catalog.core.branch_refs.is_hidden`), so a delete that dies part-way leaves a branch that still stands, and the delete is repeated.

The format describes a branch as its reference file and its directory (``lance_docs/lance/format/table/branch_tag.md``)
and says nothing of a directory without its file: that Lance then lists no such branch, opens it by name all the same,
and lists it again once the file is back is measured on pylance 14.0.3.

Every function is BLOCKING IO; callers threadpool it.
"""

import logging
from datetime import datetime

import lance
from lance_namespace import ConcurrentModificationError, InternalError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from catalog.core import branch_refs
from catalog.core.config import shared_lance_session
from catalog.core.table_objects import delete_table_object
from catalog.services.table_claims import DoorTable
from catalog.services.tables.versions import identifier_of
from lakehouse.lancekit.lance_session import open_dataset
from lakehouse.records import branch_trash, history_fences
from lakehouse.records.branch_trash import HiddenBranch
from lakehouse.records.location_claims import ClaimStore
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)


class Hiding(BaseModel):
    """What one recoverable delete stamps on every branch it hides."""

    model_config = ConfigDict(frozen=True)

    dropped_by: str
    dropped_at: datetime
    expires_at: datetime


def _cut_from(hidden: HiddenBranch) -> int | tuple[str, int]:
    """The reference of the version ``hidden`` was cut from, as pylance takes one."""
    return hidden.parent_version if hidden.parent_branch is None else (hidden.parent_branch, hidden.parent_version)


def hide(table: DoorTable, so: StorageOptions, *, name: str, listed: object, hiding: Hiding) -> None:
    """Hide the branch ``name``, which the table's listing describes as ``listed``, until ``hiding``'s deadline.

    Raises:
        ServiceUnavailableError: The store did not take the reference file away; the branch still stands.
        OSError: The store refused the record, the read of the reference file or the tag; the branch still stands.
    """
    entry = listed if isinstance(listed, dict) else {}
    hidden = HiddenBranch(
        name=name,
        location=table.location,
        reference=branch_refs.read_reference(table.location, so, name),
        parent_branch=entry.get("parent_branch"),
        parent_version=int(entry["parent_version"]),
        identifier=identifier_of(entry),
        tag=branch_trash.tag_of(name),
        dropped_by=hiding.dropped_by,
        dropped_at=hiding.dropped_at.isoformat(),
        expires_at=hiding.expires_at.isoformat(),
    )
    table.file_hidden_branch(hidden)
    dataset = table.open_unchecked(so)
    if hidden.tag not in dataset.tags.list():
        dataset.tags.create(hidden.tag, _cut_from(hidden))
    delete_table_object(table.location, so, branch_refs.reference(name))
    if branch_refs.reference_stands(table.location, so, name):
        raise ServiceUnavailableError(f"the object store did not remove the reference of branch {name!r}; delete it again")


def _release(dataset: lance.LanceDataset, hidden: HiddenBranch) -> None:
    if hidden.tag in dataset.tags.list():
        dataset.tags.delete(hidden.tag)


def restore(table: DoorTable, so: StorageOptions, hidden: HiddenBranch) -> None:
    """Put the branch ``hidden`` records back as it was: its reference file, then the tag released and the record ended.

    Raises:
        OSError: The store refused a step; the record stands, and the restore is safe to repeat.
    """
    if not branch_refs.reference_stands(hidden.location, so, hidden.name):
        branch_refs.write_reference(hidden, so)
    _release(table.open_unchecked(so), hidden)
    table.end_hidden_branch(hidden.name)


def purge(store: ClaimStore, so: StorageOptions, table_uuid: str, *, hidden: HiddenBranch) -> bool:
    """End what a recoverable delete kept of the branch ``hidden`` records; whether the branch was deleted for good.

    A hidden branch is deleted with Lance's own branch delete, which removes its directory and needs the branch listed:
    the record is marked first, then the reference file put back. A record whose hide never finished (:func:`catalog.core.branch_refs.is_hidden`)
    names a branch that stands, which is left standing with the tag released and the record ended.

    Raises:
        history_fences.HistoryFencedError: A ref write holds the table's history; the next pass purges it.
        OSError: The store refused a step; the record stands for the next pass.
    """
    history = history_fences.TableHistory(control_root=store.control_root, storage_options=store.storage_options, table_uuid=table_uuid)
    with history.held(history_fences.HistoryWork.RECLAIM):
        dataset: lance.LanceDataset = open_dataset(hidden.location, storage_options=so.in_clear(), session=shared_lance_session())
        hidden_now = branch_refs.is_hidden(hidden, so)
        if hidden_now:
            marked = hidden.model_copy(update={"purging": True})
            branch_trash.file(store.control_root, store.storage_options, table_uuid, hidden=marked)
            if not branch_refs.reference_stands(hidden.location, so, hidden.name):
                branch_refs.write_reference(hidden, so)
            if hidden.name in dataset.branches.list():
                dataset.branches.delete(hidden.name)
        _release(dataset, hidden)
    branch_trash.end(store.control_root, store.storage_options, table_uuid, name=hidden.name)
    log.info("hidden_branch_record_ended", extra={"table_uuid": table_uuid, "branch": hidden.name, "dropped_at": hidden.dropped_at, "deleted": hidden_now})
    return hidden_now


def purge_all(store: ClaimStore, so: StorageOptions, table_uuid: str) -> list[str]:
    """Delete for good every hidden branch of the table, whatever its deadline, the innermost first; their names.

    For an erasure and a deregister: a hidden branch's bytes may hold an erased subject, and no reader of the table's
    refs sees them; and a table leaving the catalog keeps no record to restore one from.

    Raises:
        ConcurrentModificationError: A ref write holds the table's history; nothing more was deleted.
        InternalError: A record of the table does not read as one, so what it keeps is unknown.
        OSError: The store refused a step; that branch's record stands.
    """
    try:
        kept = branch_trash.of_table(store.control_root, store.storage_options, table_uuid)
    except branch_trash.UnreadableHiddenBranchError as exc:
        raise InternalError(f"what was kept of this table's deleted branches cannot be read: {exc}") from exc
    purged: list[str] = []
    for hidden in sorted(kept, key=lambda kept_one: len(kept_one.name), reverse=True):
        try:
            deleted = purge(store, so, table_uuid, hidden=hidden)
        except history_fences.HistoryFencedError as exc:
            raise ConcurrentModificationError(f"the table's history is held while its deleted branches are removed: {exc}") from exc
        if deleted:
            purged.append(hidden.name)
    return purged
