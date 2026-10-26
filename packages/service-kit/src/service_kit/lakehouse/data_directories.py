"""A table's own directories on its data bases, and the one rule for which of them ending the table deletes ([[LH-286]]).

A table spreading its fragments over approved data bases keeps them in a directory of its own beneath each base
([[LH-252]]). Its base record names the directory and a location claim on the control root holds it for the table, so
no other table writes or reads there. Those bytes are the table's storage as much as its root is, and once the record
is forgotten no maintenance pass and no purge finds them again: both read a table's directories off its record. So
every path that ends a table with its bytes, the catalog's destructive drops and purging cascades and maintenance's
purge, deletes the table's directories before the record goes (:func:`end_base_record`).

ONLY A DIRECTORY THE TABLE'S CLAIM NAMES IS DELETED: a ``data`` entry on a plain base whose claim names the table, held
or released by it (:func:`last_claimed_by`). A claim naming another table, or none, as an approved base itself has where
a record was written before [[LH-252]], leaves that directory to whoever's bytes it holds, and the table still ends.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse import location_claims
from service_kit.lakehouse.base_refs import key_uri
from service_kit.lakehouse.base_registry import BaseRecord, BaseRegistry, BaseRole, RecordedBase, forget_base_record, read_base_record
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.stored_objects import delete_objects, list_under


log = logging.getLogger(__name__)


class Deleted(BaseModel):
    """What one delete of a table's data directories took."""

    model_config = ConfigDict(frozen=True)

    files: int = 0
    bytes: int = 0


def recorded_directories(record: BaseRecord | None) -> list[RecordedBase]:
    """The table's own data directories, as its base record names them: ``data`` entries on a plain base."""
    if record is None:
        return []
    return [entry for entry in record.entries if entry.role is BaseRole.DATA and not entry.is_dataset_root]


def directory_uri(directory: RecordedBase) -> str:
    """Where ``directory`` is listed: its path in the store the record names."""
    return key_uri(directory.store, directory.path)


def claimed_by(claims: location_claims.ClaimStore, directory: RecordedBase, table: str) -> bool:
    """Whether ``table`` holds ``directory``'s claim now: what maintenance may reclaim in for a live table.

    Raises:
        ValueError: The stored claim is not a claim.
        botocore.exceptions.ClientError: The store refused the read.
    """
    held = location_claims.holder(claims, directory.path)
    return held is not None and held.table == table


def last_claimed_by(claims: location_claims.ClaimStore, directory: RecordedBase, table: str) -> bool:
    """Whether ``directory``'s claim names ``table``, held or released by it: what ending that table may delete.

    A path that forgot the table's base record part-way has released the record's directory claims already
    (:func:`forget_base_record` releases them first), so the attempt that finishes it finds them released.

    Raises:
        ValueError: The stored claim is not a claim.
        botocore.exceptions.ClientError: The store refused the read.
    """
    found = location_claims.read(claims, directory.path)
    return found is not None and found.claim.table == table


def directories_to_end(registry: BaseRegistry, location: str, *, table: str) -> list[RecordedBase]:
    """The data directories ending the table at ``location`` deletes; the record's others are left, logged.

    Raises:
        UnreadableBaseRecordError: The record exists and cannot be read.
        ValueError: A stored claim is not a claim.
        botocore.exceptions.ClientError: The store refused a read.
    """
    claims = location_claims.ClaimStore(control_root=registry.control_root, storage_options=registry.storage_options)
    directories = recorded_directories(read_base_record(registry, location))
    owned = [directory for directory in directories if last_claimed_by(claims, directory, table)]
    if left := [directory.path for directory in directories if directory not in owned]:
        log.warning("data_directory_left_to_its_claim", extra={"location": location, "table": table, "directories": left})
    return owned


def delete_directories(directories: list[RecordedBase], storage_options: StorageOptions) -> Deleted:
    """Delete every object in ``directories`` by key, which writes no directory marker ([[LH-535]]).

    Raises:
        OSError: The store refused a delete; what was deleted before it stays deleted.
    """
    files = size = 0
    for directory in directories:
        uri = directory_uri(directory)
        held = list_under(uri, storage_options)
        delete_objects(uri, storage_options, [obj.path for obj in held])
        files += len(held)
        size += sum(obj.size for obj in held)
    return Deleted(files=files, bytes=size)


def delete_table_directories(registry: BaseRegistry, location: str, *, table: str, storage_options: StorageOptions) -> Deleted:
    """Delete the data directories the table at ``location`` holds, leaving the record that names them.

    For a path whose commit point comes after the delete, so a failure leaves the table, and its record, to the retry.

    Raises:
        UnreadableBaseRecordError: The record exists and cannot be read.
        OSError: The store refused a delete.
        botocore.exceptions.ClientError: The store refused a read.
    """
    return delete_directories(directories_to_end(registry, location, table=table), storage_options)


def end_base_record(registry: BaseRegistry, location: str, *, table: str, storage_options: StorageOptions) -> Deleted:
    """Delete the data directories the table at ``location`` holds, then forget its base record and their claims.

    The record goes last: it is what names the directories, so a failure part-way leaves it for the next attempt.

    Raises:
        UnreadableBaseRecordError: The record exists and cannot be read.
        OSError: The store refused a delete; the record stands.
        botocore.exceptions.ClientError: The store refused a read.
    """
    deleted = delete_table_directories(registry, location, table=table, storage_options=storage_options)
    forget_base_record(registry, location, table=table)
    return deleted
