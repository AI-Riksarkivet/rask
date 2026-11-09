"""What each file a commit adds was when the catalog committed it, so an overwrite in place is found later ([[LH-419]]).

A writer's vend may put any object under the paths a commit needs (``table_layout.WRITER_PUT_GLOBS``), and a session
policy cannot refuse a put over an existing key, so a writer can replace a committed data, deletion, index or blob
sidecar file. Lance requires a published payload to stay immutable while a protected snapshot references it
(``lance_docs/lance/format/table/blob.md:254-257``) and checks none: measured on pylance 13.0.0, a same-size overwrite of a
data file is served silently. Every catalog commit therefore records, before its version lands, each file it adds under
the table's own root with the ETag and size the store answers, and each pass of the table reads every recorded file
again (``maintenance.services.committed_files``). A file a commit adds through a base is not recorded: no vend may put
there.

ONE RECORD PER COMMIT (``_file_identities/<table uuid>/<transaction uuid>.json``), keyed by the table's uuid so a
rename moves none and a table created later under the name inherits none (ADR 0173 §8), and by the commit's transaction
uuid, which a commit that loses its race shares with no other. Each path is relative to the table's root, so a branch's
files are ``tree/<branch>/...``. What is recorded is read before a stock commit's files are judged, so a file replaced
after that read is found, and one replaced before it is the file judged and committed. A file the catalog writes itself
is named by a uuid and replaced unseen only between its write and that read, a window this record cannot close.

MAINTENANCE KEEPS A COPY of the records it has read (``_maintenance_files/<table uuid>.json``, under its own identity), so
a pass reads only the records written since its last ([[LH-494]]): a listing of the records and the copy, then a read
of each record the copy lacks. The records stay the catalog's and the authority; the copy is a cache of maintenance's
own check, which no erasure, hold, ref or fence reads, and it ends with them (:func:`end`).

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse import stored_objects
from lakehouse.objectfs import fs_and_base
from lakehouse.record_store import delete_record, get_record, put_record
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

PREFIX: Final = "_file_identities"
#: Maintenance's copy of each table's records, one object per table.
COPY_PREFIX: Final = "_maintenance_files"


class CommittedFile(BaseModel):
    """One file a commit added, as the store held it when the commit was recorded."""

    model_config = ConfigDict(frozen=True)

    #: Relative to the table's root.
    path: str
    identity: stored_objects.ObjectIdentity


class CommittedFiles(BaseModel):
    """Every file one commit added under its table's root."""

    model_config = ConfigDict(frozen=True)

    transaction_uuid: str
    files: list[CommittedFile]


class _CopiedRecord(BaseModel):
    """One record as maintenance read it: its size as the store listed it, and what it holds."""

    model_config = ConfigDict(frozen=True)

    size: int
    committed: CommittedFiles


class _RecordCopy(BaseModel):
    """Maintenance's copy of one table's records, by transaction uuid."""

    model_config = ConfigDict(frozen=True)

    records: dict[str, _CopiedRecord] = Field(default_factory=dict)


def records_of(table_uuid: str) -> str:
    """Where the table ``table_uuid``'s records are kept on the control root, one object per commit beneath it."""
    return f"{PREFIX}/{table_uuid}"


def _key(table_uuid: str, transaction_uuid: str) -> str:
    return f"{records_of(table_uuid)}/{transaction_uuid}.json"


def _copy_key(table_uuid: str) -> str:
    return f"{COPY_PREFIX}/{table_uuid}.json"


def as_stored(table_root: str, storage_options: StorageOptions, transaction_uuid: str, paths: Sequence[str]) -> CommittedFiles:
    """The files at ``paths`` under ``table_root`` (the store's own spelling of it) as the store holds them now.

    A path the store does not hold is left out: ``version/create`` refuses a commit naming one, and an object nobody
    wrote holds no committed bytes to replace.

    Raises:
        OSError: The store refused, or could not be reached for, a read.
    """
    found = stored_objects.identities(table_root, storage_options, paths)
    files = [CommittedFile(path=path, identity=found[path]) for path in paths if path in found]
    return CommittedFiles(transaction_uuid=transaction_uuid, files=files)


def record(control_root: str, storage_options: StorageOptions, table_uuid: str, committed: CommittedFiles) -> None:
    """Record what one commit of the table ``table_uuid`` added; nothing for a commit that added no file."""
    if committed.files:
        put_record(control_root, storage_options, _key(table_uuid, committed.transaction_uuid), committed.model_dump())


def recorded_and_copied(control_root: str, storage_options: StorageOptions, table_uuid: str) -> list[CommittedFiles]:
    """Every commit of the table ``table_uuid`` that recorded what it added, for a listing of its records and a read of
    maintenance's copy: a record is read only when the copy lacks it as listed, and the copy is written again whenever the
    listing changed it.

    A record's key names its content while it is listed: only the catalog puts one, once per commit, before the version
    lands, and a vend may read and delete one but never put one (``catalog.core.vending``). Its listed size is kept beside
    it all the same, so a record put again with other content is read again. A copy that no longer parses is rebuilt.

    Raises:
        ValueError: A record is not one a commit wrote: a file it names could then never be checked.
        OSError: The store refused, or could not be reached for, the listing, a read or the copy's write.
    """
    copy = _read_copy(control_root, storage_options, table_uuid)
    records: dict[str, _CopiedRecord] = {}
    for transaction_uuid, size in listed_records(control_root, storage_options, table_uuid).items():
        copied = copy.records.get(transaction_uuid)
        if copied is None or copied.size != size:
            committed = _read_record(control_root, storage_options, _key(table_uuid, transaction_uuid))
            if committed is None:
                continue
            copied = _CopiedRecord(size=size, committed=committed)
        records[transaction_uuid] = copied
    if records != copy.records:
        put_record(control_root, storage_options, _copy_key(table_uuid), _RecordCopy(records=records).model_dump())
    return [copied.committed for copied in records.values()]


def listed_records(control_root: str, storage_options: StorageOptions, table_uuid: str) -> dict[str, int]:
    """Each record of the table ``table_uuid`` the store lists, by transaction uuid: its size. Reads only.

    Raises:
        OSError: The store refused, or could not be reached for, the listing.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{records_of(table_uuid)}", storage_options)
    except (BotoCoreError, ClientError) as exc:
        raise OSError(f"the store refused, or could not be reached for, listing {records_of(table_uuid)}: {exc}") from exc
    return {name.removesuffix(".json"): stored.size for stored in listed if (name := stored.path.rsplit("/", 1)[-1]).endswith(".json")}


def _read_copy(control_root: str, storage_options: StorageOptions, table_uuid: str) -> _RecordCopy:
    held = get_record(control_root, storage_options, _copy_key(table_uuid), event="file_identities_copy")
    if held is None:
        return _RecordCopy()
    try:
        return _RecordCopy.model_validate(held)
    except ValidationError:
        log.warning("file_identities_copy_rebuilt", extra={"table_uuid": table_uuid})
        return _RecordCopy()


def _read_record(control_root: str, storage_options: StorageOptions, key: str) -> CommittedFiles | None:
    """The record at ``key``, or ``None`` once it is forgotten.

    Raises:
        ValueError: The record is not one a commit wrote.
    """
    fs, base = fs_and_base(control_root, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{key}") as stream:
            return CommittedFiles.model_validate_json(stream.readall())
    except FileNotFoundError:
        return None


def forget(control_root: str, storage_options: StorageOptions, table_uuid: str, transaction_uuid: str) -> None:
    """Delete one commit's record: once no retained version names its files and the store holds none of them, or once the
    commit is proven never to land, when no version names them whatever the store still holds ([[LH-564]])."""
    delete_record(control_root, storage_options, _key(table_uuid, transaction_uuid))


def end(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every record of the table ``table_uuid``, and maintenance's copy of them; whether any record existed. Writes
    no directory marker (:func:`stored_objects.delete_objects`), since maintenance, which ends a purged table's records,
    may put none here."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{records_of(table_uuid)}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    delete_record(control_root, storage_options, _copy_key(table_uuid))
    return bool(listed)
