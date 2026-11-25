"""What the catalog keeps beside a table's version log so that every committed version is announced ([[LH-497]]).

Two control-root registries, both keyed by the table's uuid, so a rename moves neither and a table created later under
the name inherits neither (ADR 0173 §8):

- **The author the catalog authorized each commit for** (``_commit_authors/<table uuid>/<transaction uuid>/<version>.json``,
  ADR 0187). Every commit of an operation the doors announce that a catalog door makes or ``version/create`` admits
  records its verified caller here, with the ref it was made on: the reconciler signs a re-announcement only for the
  author recorded for that transaction at that ref and version, never for the ``rask.author`` a version's transaction
  properties carry, which any writer that can put a final manifest can stamp. A transaction's uuid alone would not do:
  every reader of the table sees it, and a manifest put past every door can carry it again at another version. A stock
  client chooses its transaction's uuid and keeps it when it stages the commit again on a later version, so each
  version it is made for has a record of its own, written once: one recorded for another caller, or on another ref,
  is never recorded again. A record written before its commit's finalize also names the manifest the catalog judged and
  finalizes (:func:`manifest_digest`): a writer past every door can read the uuid off the commit's staged transaction and
  land that version first with other content, which then matches no record.
- **The commits a door was about to make** (``_commit_pending/<table uuid>/<entry>.json``, [[XC-204]]). A door files one
  entry before a commit it announces can land, naming the location its table is claimed at, so the reconciler reads the
  tables that took a commit since it last looked and no other: an idle table costs a pass nothing. An entry is removed by
  the pass that read its table once it is older than the door's lease and the reconciler's grace together, when the door
  has stopped and any version it landed is old enough to have been looked for.
- **The reconciler's mark** (``_commit_marks/table-<digest>.json``): for each ref of the table, the highest version up
  to which no version is left to announce, so a pass reads only the versions after it. A registered table's first mark
  is its register's, at each ref's head: the versions a register attaches were committed past every door ([[LH-550]]).

Every function is BLOCKING IO; callers threadpool it.
"""

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from lakehouse.records import control_prefixes
from lakehouse.records.record_store import UnreadableRecordError, delete_record, get_record, put_record, read_record, record_key
from lakehouse.storage import conditional_records, stored_objects
from lakehouse.storage.options import StorageOptions


__all__ = [
    "AUTHORS_PREFIX",
    "MARKS_PREFIX",
    "PENDING_PREFIX",
    "AuthorRecordedError",
    "CommitAuthor",
    "CommitMarks",
    "InvalidTransactionUuidError",
    "PendingTable",
    "UnreadableAuthorRecordError",
    "end_authors",
    "file_pending",
    "forget_author",
    "is_canonical_uuid",
    "manifest_digest",
    "pending_tables",
    "read_marks",
    "record_author",
    "recorded",
    "ref_key",
    "settle_pending",
    "write_marks",
]


AUTHORS_PREFIX: Final = control_prefixes.COMMIT_AUTHORS
MARKS_PREFIX: Final = control_prefixes.COMMIT_MARKS
PENDING_PREFIX: Final = control_prefixes.COMMIT_PENDING

log = logging.getLogger(__name__)


class CommitAuthor(BaseModel):
    """The caller the catalog authorized one commit for, at the ref and version it was made for."""

    model_config = ConfigDict(frozen=True)

    author: str
    transaction_uuid: str
    #: The ref's key (:func:`ref_key`): ``""`` for main, else the branch and its incarnation.
    ref: str
    version: int
    #: The :func:`manifest_digest` of the manifest the catalog judged and finalizes, for a record written before its
    #: commit's finalize; ``None`` for one read off the version once it landed.
    manifest: str | None = None


def manifest_digest(manifest: bytes) -> str:
    """The SHA-256 a record names a manifest by, hex."""
    return hashlib.sha256(manifest).hexdigest()


class InvalidTransactionUuidError(ValueError):
    """A transaction names a uuid that is not one in its canonical form, so no record is keyed by it."""


def is_canonical_uuid(value: str) -> bool:
    """Whether ``value`` is a uuid spelled as Lance mints a transaction's: lower-case hex in hyphenated groups. A stock
    client chooses its own transaction's uuid, and a record's key is built from it."""
    try:
        return str(uuid.UUID(value)) == value
    except ValueError, TypeError, AttributeError:
        return False


def _author_key(table_uuid: str, transaction_uuid: str, version: int) -> str:
    if not is_canonical_uuid(transaction_uuid):
        raise InvalidTransactionUuidError(f"transaction uuid {transaction_uuid!r} is not a canonical uuid")
    return f"{AUTHORS_PREFIX}/{table_uuid}/{transaction_uuid}/{version}.json"


class UnreadableAuthorRecordError(UnreadableRecordError):
    """Raised when an author record is filed for the commit and does not read as one, so whether a door authorized it is
    unknown: it signs nothing, and the version is not passed as one no door made. Names the key it is filed under."""


class AuthorRecordedError(Exception):
    """The transaction is recorded at the version already, for another author or on another ref: it names another
    caller's commit."""


def record_author(
    control_root: str, storage_options: StorageOptions, table_uuid: str, *, transaction_uuid: str, version: int, ref: str, author: str, manifest: str | None
) -> bool:
    """Record ``author`` as the caller the commit of ``transaction_uuid`` at ``version`` of the ref ``ref`` was authorized for,
    on the table ``table_uuid``, its manifest ``manifest`` (:class:`CommitAuthor`); whether this call created the record.

    Created only where none is recorded. Recording the same author on the same ref again is a retry and changes nothing:
    the record keeps the manifest it named, so a retry that lands other bytes matches it nowhere.

    Raises:
        InvalidTransactionUuidError: ``transaction_uuid`` is not a canonical uuid.
        AuthorRecordedError: The transaction is recorded at ``version`` for another author or on another ref.
        UnreadableAuthorRecordError: A record is kept for the transaction at ``version`` and does not read as one.
        OSError: The store refused, or could not be reached for, the write or the read.
    """
    record = CommitAuthor(author=author, transaction_uuid=transaction_uuid, ref=ref, version=version, manifest=manifest)
    key = _author_key(table_uuid, transaction_uuid, version)
    try:
        conditional_records.create_json(control_root, storage_options, key, record=record.model_dump())
    except conditional_records.RecordExistsError:
        recorded = _read(control_root, storage_options, key)
        if recorded is None or (recorded.author, recorded.ref) != (author, ref):
            raise AuthorRecordedError(f"transaction {transaction_uuid} of table {table_uuid} is recorded at version {version} for another commit") from None
        return False
    return True


def _read(control_root: str, storage_options: StorageOptions, key: str) -> CommitAuthor | None:
    """The record at ``key``, or ``None`` when none is kept.

    Raises:
        UnreadableAuthorRecordError: A record is kept there and does not read as one.
        OSError: The store refused, or could not be reached for, the read.
    """
    try:
        loaded = read_record(control_root, storage_options, key)
        return CommitAuthor.model_validate(loaded) if loaded is not None else None
    except ValueError as exc:  # not text, not JSON, not an object, or not an author record (pydantic's error is one)
        raise UnreadableAuthorRecordError(f"the author record at {key} cannot be read: {str(exc)[:200]}") from exc


def recorded(control_root: str, storage_options: StorageOptions, table_uuid: str, *, transaction_uuid: str, version: int, ref: str) -> CommitAuthor | None:
    """The record of the commit of ``transaction_uuid`` at ``version`` of the ref ``ref``; ``None`` when none is kept, when
    the record names another ref, or when ``transaction_uuid`` is no canonical uuid, which no record is keyed by.

    Raises:
        UnreadableAuthorRecordError: A record is kept for the commit and does not read as one.
        OSError: The store refused, or could not be reached for, the read.
    """
    try:
        key = _author_key(table_uuid, transaction_uuid, version)
    except InvalidTransactionUuidError:
        return None
    found = _read(control_root, storage_options, key)
    if found is None or (found.transaction_uuid, found.version, found.ref) != (transaction_uuid, version, ref):
        return None
    return found


def forget_author(control_root: str, storage_options: StorageOptions, table_uuid: str, *, transaction_uuid: str, version: int) -> None:
    """Delete the author record of the commit of ``transaction_uuid`` at ``version``, if one is kept.

    Raises:
        OSError: The store refused the delete.
    """
    try:
        key = _author_key(table_uuid, transaction_uuid, version)
    except InvalidTransactionUuidError:
        return
    delete_record(control_root, storage_options, key)


def end_authors(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every author record of the table ``table_uuid``; whether any existed. By key, as the table's other records
    end (:func:`stored_objects.delete_objects`): the purge ends them under a session reaching the records' own keys alone,
    which a probe of the bare directory key and the marker a directory delete puts back both fall outside ([[XC-084]])."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{AUTHORS_PREFIX}/{table_uuid}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)


class CommitMarks(BaseModel):
    """For each ref of one table, the highest version up to which no version is left to announce: each is on the feed, is
    one no door announces, was counted unauthored, is past the horizon, or was committed before the table's register.

    A ref is keyed by its name and its incarnation, ``""`` for main: a deleted and recreated branch restarts its
    numbering, so a mark kept for its earlier incarnation says nothing about the new one.
    """

    model_config = ConfigDict(frozen=True)

    refs: dict[str, int] = {}


def ref_key(branch: str | None, branch_identifier: str | None) -> str:
    """The key a ref's mark is kept under: ``""`` for main, else ``<branch>@<incarnation>``."""
    return "" if branch is None else f"{branch}@{branch_identifier or ''}"


def read_marks(control_root: str, storage_options: StorageOptions, table_uuid: str) -> CommitMarks:
    """The table's marks; none when it has no record, or one that does not read as marks."""
    loaded = get_record(control_root, storage_options, record_key(MARKS_PREFIX, "table", table_uuid), event="commit_marks")
    if loaded is None:
        return CommitMarks()
    try:
        return CommitMarks.model_validate(loaded)
    except ValidationError:
        return CommitMarks()


def write_marks(control_root: str, storage_options: StorageOptions, table_uuid: str, *, marks: CommitMarks) -> None:
    """Replace the table's marks. Two passes racing may write a lower mark over a higher one, which costs a re-read of
    versions already announced and never skips one: every mark a pass writes is one it verified."""
    put_record(control_root, storage_options, record_key(MARKS_PREFIX, "table", table_uuid), record=marks.model_dump())


class PendingTable(BaseModel):
    """One table a door was about to commit to, as the entries filed for it read."""

    model_config = ConfigDict(frozen=True)

    table_uuid: str
    #: Where the table was claimed when a door filed an entry; ``None`` when no entry of the table reads as one, which
    #: says nothing of where the table is: its entries stand.
    location: str | None
    #: The store paths of the table's entries old enough to remove (:func:`settle_pending`).
    lapsed: list[str]


def file_pending(control_root: str, storage_options: StorageOptions, table_uuid: str, *, location: str) -> None:
    """File that a door is about to commit to the table ``table_uuid``, claimed at ``location``.

    Raises:
        OSError: The store refused the entry, so the door must not commit: a commit no entry names is one the reconciler
            does not look for.
    """
    put_record(control_root, storage_options, f"{PENDING_PREFIX}/{table_uuid}/{uuid.uuid4().hex}.json", record={"location": location})


def pending_tables(control_root: str, storage_options: StorageOptions, *, lapse: timedelta) -> list[PendingTable]:
    """Every table an entry is filed for, each with its entries older than ``lapse`` on the store's own clock.

    One listing of the prefix, a thousand entries a request, and one read a table.

    Raises:
        OSError: The store refused the listing.
    """
    root = control_root.rstrip("/")
    listing = stored_objects.list_under_at(f"{root}/{PENDING_PREFIX}", storage_options)
    entries: dict[str, list[stored_objects.StoredObject]] = {}
    for stored in listing.objects:
        entries.setdefault(stored.path.rsplit("/", 2)[-2], []).append(stored)
    return [
        PendingTable(
            table_uuid=table_uuid,
            location=_pending_location(control_root, storage_options, table_uuid, filed=filed),
            lapsed=[stored.path for stored in filed if listing.at - stored.modified >= lapse],
        )
        for table_uuid, filed in entries.items()
    ]


def _pending_location(control_root: str, storage_options: StorageOptions, table_uuid: str, *, filed: list[stored_objects.StoredObject]) -> str | None:
    """The location the first entry of ``filed`` that reads as one names; ``None``, logged, when none does."""
    for stored in filed:
        key = f"{PENDING_PREFIX}/{table_uuid}/{stored.path.rsplit('/', 1)[-1]}"
        try:
            record = read_record(control_root, storage_options, key)
        except UnreadableRecordError:
            continue
        if record is not None and isinstance(record.get("location"), str):
            return record["location"]
    log.error("commit_pending_unreadable", extra={"table_uuid": table_uuid, "entries": len(filed)})
    return None


def settle_pending(control_root: str, storage_options: StorageOptions, paths: list[str]) -> None:
    """Remove the entries at the store ``paths`` (:attr:`PendingTable.lapsed`).

    Raises:
        OSError: The store refused a delete.
    """
    stored_objects.delete_objects(control_root, storage_options, paths)
