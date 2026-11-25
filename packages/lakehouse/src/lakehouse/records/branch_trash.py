"""The branches a recoverable delete hid, kept whole until their deadline ([[LH-429]]).

Deleting a Lance branch removes ``tree/<branch>/`` with every commit made on it, and recreating it where it was cut
restores none of them (measured on pylance 14.0.3). So where a grace period is in force a delete HIDES the branch
instead: the catalog takes its reference file away (``_refs/branches/<name>.json``,
``lance_docs/lance/format/table/branch_tag.md`` § Branch Metadata Path), so no listing names it, and keeps the file's own
bytes in a record here, one per branch under its table's uuid. A restore puts the file back; the deadline deletes the
branch for good.

A hidden branch's reference no longer holds the version it was cut from against the parent's cleanup: measured, a
cleanup of an overwritten parent then deletes files the branch reads, and the restored branch fails ``Not found``. So
the record names a tag (:func:`tag_of`) the catalog holds on that version for as long as the record stands: a cleanup
removes no tagged version (``lance_docs/lance/guide/tags_and_branches.md``: "To remove a version that has been tagged,
you must first delete the tag").

Every function is BLOCKING IO; callers threadpool it.
"""

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from lakehouse.records import control_prefixes
from lakehouse.records.record_store import UnreadableRecordError, delete_record, put_record, read_record
from lakehouse.storage import stored_objects
from lakehouse.storage.options import StorageOptions


__all__ = ["PREFIX", "TAG_PREFIX", "HiddenBranch", "UnreadableHiddenBranchError", "end", "end_table", "file", "get", "of_every_table", "of_table", "tag_of"]


log = logging.getLogger(__name__)

PREFIX: Final = control_prefixes.BRANCH_TRASH
#: The tags the catalog holds on the versions hidden branches were cut from; no door writes or deletes one for a caller.
TAG_PREFIX: Final = "rask.trash."


class UnreadableHiddenBranchError(UnreadableRecordError):
    """Raised when a record is filed for a hidden branch and does not read as one: whether the name is hidden, and what
    a restore would put back, are unknown, so it is never read as absent."""


class HiddenBranch(BaseModel):
    """One branch a recoverable delete hid."""

    model_config = ConfigDict(frozen=True)

    name: str
    #: The table's own location, which the deadline's delete opens: a rename moves a name and never a location.
    location: str
    #: The reference file's own text, which a restore writes back unchanged.
    reference: str
    #: The ref the branch was cut from (``None`` for main) and the version on it, which :attr:`tag` holds.
    parent_branch: str | None
    parent_version: int
    #: The incarnation the branch was, as its listing named it.
    identifier: str | None
    tag: str
    dropped_by: str
    dropped_at: str
    #: When the branch is deleted for good, as the delete stamped it.
    expires_at: str
    #: Set before the delete for good puts the reference file back for Lance's own branch delete: a reference file beside
    #: a record without it is a hide that never finished, whose branch still stands.
    purging: bool = False

    def past_deadline(self, now: datetime) -> bool:
        """Whether the deadline has passed at ``now``."""
        return now >= datetime.fromisoformat(self.expires_at)


def tag_of(name: str) -> str:
    """The tag the catalog holds on the version the branch ``name`` was cut from while it is hidden."""
    return f"{TAG_PREFIX}{hashlib.sha256(name.encode()).hexdigest()[:32]}"


def _key(table_uuid: str, name: str) -> str:
    return f"{PREFIX}/{table_uuid}/{hashlib.sha256(name.encode()).hexdigest()}.json"


def file(control_root: str, storage_options: StorageOptions, table_uuid: str, *, hidden: HiddenBranch) -> None:
    """Record that the branch ``hidden`` names is hidden; a record a delete that died left under the name is replaced.

    Raises:
        OSError: The store refused the record.
    """
    put_record(control_root, storage_options, _key(table_uuid, hidden.name), record=hidden.model_dump())


def get(control_root: str, storage_options: StorageOptions, table_uuid: str, *, name: str) -> HiddenBranch | None:
    """The record of the hidden branch ``name``, or ``None`` when none is filed.

    Raises:
        UnreadableHiddenBranchError: A record is filed and does not read as one.
        OSError: The store refused the read.
    """
    key = _key(table_uuid, name)
    try:
        raw = read_record(control_root, storage_options, key)
        return None if raw is None else HiddenBranch.model_validate(raw)
    except (UnreadableRecordError, ValidationError) as exc:
        raise UnreadableHiddenBranchError(f"the hidden-branch record {key} does not read as one") from exc


def of_table(control_root: str, storage_options: StorageOptions, table_uuid: str) -> list[HiddenBranch]:
    """Every branch of the table ``table_uuid`` that is hidden, by name.

    Raises:
        UnreadableHiddenBranchError: A record is filed and does not read as one.
        OSError: The store refused the listing or a read.
    """
    found = _under(control_root, storage_options, f"{PREFIX}/{table_uuid}", unreadable=_refuse_unreadable)
    return sorted(found.get(table_uuid, []), key=lambda hidden: hidden.name)


def of_every_table(control_root: str, storage_options: StorageOptions) -> dict[str, list[HiddenBranch]]:
    """Every hidden branch on the control root, by its table's uuid. A record that does not read as one is logged and
    left out, so one bad record holds up no other table's deadline.

    Raises:
        OSError: The store refused the listing or a read.
    """
    return _under(control_root, storage_options, PREFIX, unreadable=_log_unreadable)


def _refuse_unreadable(key: str, exc: Exception) -> None:
    raise UnreadableHiddenBranchError(f"the hidden-branch record {key} does not read as one") from exc


def _log_unreadable(key: str, exc: Exception) -> None:
    log.error("hidden_branch_record_unreadable", extra={"key": key, "error": str(exc)[:300]})


def _under(control_root: str, storage_options: StorageOptions, prefix: str, *, unreadable: Callable[[str, Exception], None]) -> dict[str, list[HiddenBranch]]:
    found: dict[str, list[HiddenBranch]] = {}
    for stored in stored_objects.list_under(f"{control_root.rstrip('/')}/{prefix}", storage_options):
        table_uuid, file_name = stored.path.rsplit("/", 2)[-2:]
        key = f"{PREFIX}/{table_uuid}/{file_name}"
        try:
            raw = read_record(control_root, storage_options, key)
            if raw is not None:
                found.setdefault(table_uuid, []).append(HiddenBranch.model_validate(raw))
        except (UnreadableRecordError, ValidationError) as exc:
            unreadable(key, exc)
    return found


def end(control_root: str, storage_options: StorageOptions, table_uuid: str, *, name: str) -> None:
    """Delete the record of the hidden branch ``name``, if one is filed.

    Raises:
        OSError: The store refused the delete.
    """
    delete_record(control_root, storage_options, _key(table_uuid, name))


def end_table(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every hidden-branch record of the table ``table_uuid``, whose bytes end with the table; whether any existed."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{PREFIX}/{table_uuid}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)
