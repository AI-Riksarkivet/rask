"""What each file a commit adds was when the catalog committed it, so an overwrite in place is found later ([[LH-419]]).

A writer's vend may put any object under the paths a commit needs (``table_layout.WRITER_PUT_GLOBS``), and a session
policy cannot refuse a put over an existing key, so a writer can replace a committed data, deletion, index or blob
sidecar file. Lance requires a published payload to stay immutable while a protected snapshot references it
(``lance_docs/lance/format/table/blob.md:254-257``) and checks none: measured on pylance 13.0.0, a same-size overwrite of a
data file is served silently. Every catalog commit therefore records, before its version lands, each file it adds under
the table's own root with the ETag and size the store answers, and maintenance's sweep reads each recorded file again
(``maintenance.services.committed_files``). A file a commit adds through a base is not recorded: no vend may put there.

ONE RECORD PER COMMIT (``_file_identities/<table uuid>/<transaction uuid>.json``), keyed by the table's uuid so a
rename moves none and a table created later under the name inherits none (ADR 0173 §8), and by the commit's transaction
uuid, which a commit that loses its race shares with no other. Each path is relative to the table's root, so a branch's
files are ``tree/<branch>/...``. What is recorded is read before a stock commit's files are judged, so a file replaced
after that read is found, and one replaced before it is the file judged and committed. A file the catalog writes itself
is named by a uuid and replaced unseen only between its write and that read, a window this record cannot close.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse import stored_objects
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, list_records, put_record


PREFIX: Final = "_file_identities"


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


def _key(table_uuid: str, transaction_uuid: str) -> str:
    return f"{PREFIX}/{table_uuid}/{transaction_uuid}.json"


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


def recorded(control_root: str, storage_options: StorageOptions, table_uuid: str) -> list[CommittedFiles]:
    """Every commit of the table ``table_uuid`` that recorded what it added.

    Raises:
        ValueError: A record is not one a commit wrote: a file it names could then never be checked.
        OSError: The store refused, or could not be reached for, a read.
    """
    return [
        CommittedFiles.model_validate(loaded)
        for loaded in list_records(control_root, storage_options, f"{PREFIX}/{table_uuid}", event="file_identities", strict=True)
    ]


def forget(control_root: str, storage_options: StorageOptions, table_uuid: str, transaction_uuid: str) -> None:
    """Delete one commit's record, once no retained version names its files and the store holds none of them."""
    delete_record(control_root, storage_options, _key(table_uuid, transaction_uuid))


def end(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every record of the table ``table_uuid``; whether any existed. Writes no directory marker
    (:func:`stored_objects.delete_objects`), since maintenance, which ends a purged table's records, may put none here."""
    listed = stored_objects.list_under(f"{control_root.rstrip('/')}/{PREFIX}/{table_uuid}", storage_options)
    stored_objects.delete_objects(control_root, storage_options, [stored.path for stored in listed])
    return bool(listed)
