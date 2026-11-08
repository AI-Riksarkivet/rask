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
- **The reconciler's mark** (``_commit_marks/table-<digest>.json``): for each ref of the table, the highest version up
  to which no version is left to announce, so a pass reads only the versions after it. A registered table's first mark
  is its register's, at each ref's head: the versions a register attaches were committed past every door ([[LH-550]]).
- **The caller the catalog authorized each move of a tag for** (``_commit_authors/<table uuid>/tags/<tag>/<version>.json``,
  [[LH-552]]): a publication moves the table's ``published`` tag and commits no version, so it has no transaction to
  name its author. The door records its verified caller right before the tag moves, and the reconciler signs a lost
  publication's announcement only for that caller, never for whoever can write the tag file.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from lakehouse import records, stored_objects
from lakehouse.record_store import delete_record, get_record, list_records, put_record, record_key
from lakehouse.storage_options import StorageOptions


AUTHORS_PREFIX: Final = "_commit_authors"
MARKS_PREFIX: Final = "_commit_marks"


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
    except (ValueError, TypeError, AttributeError):
        return False


def _author_key(table_uuid: str, transaction_uuid: str, version: int) -> str:
    if not is_canonical_uuid(transaction_uuid):
        raise InvalidTransactionUuidError(f"transaction uuid {transaction_uuid!r} is not a canonical uuid")
    return f"{AUTHORS_PREFIX}/{table_uuid}/{transaction_uuid}/{version}.json"


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
        OSError: The store refused, or could not be reached for, the write or the read.
    """
    record = CommitAuthor(author=author, transaction_uuid=transaction_uuid, ref=ref, version=version, manifest=manifest)
    key = _author_key(table_uuid, transaction_uuid, version)
    try:
        records.create_json(control_root, storage_options, key, record.model_dump())
    except records.RecordExistsError:
        recorded = _read(control_root, storage_options, key)
        if recorded is None or (recorded.author, recorded.ref) != (author, ref):
            raise AuthorRecordedError(f"transaction {transaction_uuid} of table {table_uuid} is recorded at version {version} for another commit") from None
        return False
    except Exception as exc:
        if _from_the_store(exc):
            raise OSError(f"the store refused recording the author of transaction {transaction_uuid}: {exc}") from exc
        raise
    return True


def _from_the_store(exc: Exception) -> bool:
    """Whether ``exc`` is the S3 client's answer for a store that refused or did not answer, which every caller here reads
    as an ``OSError`` like pyarrow's."""
    from botocore.exceptions import BotoCoreError, ClientError

    return isinstance(exc, ClientError | BotoCoreError)


def _read(control_root: str, storage_options: StorageOptions, key: str) -> CommitAuthor | None:
    """The record at ``key``; ``None`` when none is kept or it does not read as one, which signs nothing and is never taken
    for a retry's."""
    try:
        loaded = get_record(control_root, storage_options, key, event="commit_author")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if loaded is None:
        return None
    try:
        return CommitAuthor.model_validate(loaded)
    except ValidationError:
        return None


def recorded(control_root: str, storage_options: StorageOptions, table_uuid: str, *, transaction_uuid: str, version: int, ref: str) -> CommitAuthor | None:
    """The record of the commit of ``transaction_uuid`` at ``version`` of the ref ``ref``; ``None`` when none is kept, when
    the record names another ref, or when ``transaction_uuid`` is no canonical uuid, which no record is keyed by.

    Raises:
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


class TagMover(BaseModel):
    """The caller the catalog authorized a move of one tag to one version for."""

    model_config = ConfigDict(frozen=True)

    author: str
    tag: str
    version: int
    #: When the door recorded it, right before the tag moved: a record kept for a move that did not land is forgotten
    #: only once it is older than the reconciler's grace, so a move in flight is never mistaken for one.
    recorded_at: datetime


def _tag_moves(table_uuid: str, tag: str) -> str:
    return f"{AUTHORS_PREFIX}/{table_uuid}/tags/{tag}"


def record_tag_move(control_root: str, storage_options: StorageOptions, table_uuid: str, *, tag: str, version: int, author: str) -> None:
    """Record ``author`` as the caller the move of ``tag`` to ``version`` on the table ``table_uuid`` was authorized for,
    right before the tag moves. A record kept for the version is replaced: the caller moving the tag now is its author.

    Raises:
        OSError: The store refused the write.
    """
    record = TagMover(author=author, tag=tag, version=version, recorded_at=datetime.now(UTC))
    put_record(control_root, storage_options, f"{_tag_moves(table_uuid, tag)}/{version}.json", record.model_dump(mode="json"))


def tag_movers(control_root: str, storage_options: StorageOptions, table_uuid: str, *, tag: str) -> list[TagMover]:
    """Every move of ``tag`` the catalog recorded on the table ``table_uuid`` and has not forgotten.

    Read whole: a reconciler that finds no record for a move counts it as made past every door, so a record it could
    not read must stop it rather than read as no record.

    Raises:
        OSError: A record could not be read.
        ValueError: A record is not one (pydantic's ``ValidationError`` is one).
    """
    loaded = list_records(control_root, storage_options, _tag_moves(table_uuid, tag), event="tag_mover", strict=True)
    return [TagMover.model_validate(record) for record in loaded]


def forget_tag_move(control_root: str, storage_options: StorageOptions, table_uuid: str, *, tag: str, version: int) -> None:
    """Delete the record of the move of ``tag`` to ``version``, if one is kept.

    Raises:
        OSError: The store refused the delete.
    """
    delete_record(control_root, storage_options, f"{_tag_moves(table_uuid, tag)}/{version}.json")


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
    Under ``tag:published``, the move of the table's publication the reconciler settled, as its ``updatedAt`` in
    microseconds since the epoch ([[LH-552]]).

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


def write_marks(control_root: str, storage_options: StorageOptions, table_uuid: str, marks: CommitMarks) -> None:
    """Replace the table's marks. Two passes racing may write a lower mark over a higher one, which costs a re-read of
    versions already announced and never skips one: every mark a pass writes is one it verified."""
    put_record(control_root, storage_options, record_key(MARKS_PREFIX, "table", table_uuid), marks.model_dump())
