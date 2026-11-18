"""Control-root records a test puts in place before the door it drives runs, and one it reads back.

Each write is whole and unconditional, which no door's is: a door creates a record on its absence and changes it on
its ETag, so these stand for the state an earlier request left, never for a request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lakehouse import control_prefixes, maintenance_due, maintenance_policies, trash
from lakehouse.maintenance_due import DueRecord
from lakehouse.record_store import put_record, record_key
from lakehouse.storage_options import StorageOptions


def put_warehouse(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Hold ``record`` as the warehouse ``record["id"]``'s registry record."""
    put_record(control_root, storage_options, f"{control_prefixes.WAREHOUSES}/{record['id']}.json", record)


def put_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Hold ``record`` as the project ``record["id"]``'s registry record."""
    put_record(control_root, storage_options, f"{control_prefixes.PROJECTS}/{record['id']}.json", record)


def policy(record: dict[str, Any] | maintenance_policies.PolicyRecord) -> maintenance_policies.PolicyRecord:
    """``record`` as the policy record the registry would read it as."""
    return record if isinstance(record, maintenance_policies.PolicyRecord) else maintenance_policies.read(record, at="a record of this test's")


def put_policy(control_root: str, storage_options: StorageOptions, record: dict[str, Any] | maintenance_policies.PolicyRecord) -> None:
    """Hold ``record`` as the maintenance policy of the object it names, under the key the policy door writes; a map is
    held as it is, whatever it holds."""
    if isinstance(record, maintenance_policies.PolicyRecord):
        record = record.stored()
    keyed_by = "uuid" if record["kind"] in ("table", "namespace") else "id"
    put_record(control_root, storage_options, record_key(maintenance_policies.PREFIX, record["kind"], str(record[keyed_by])), record)


def put_cadence_stamp(
    control_root: str, storage_options: StorageOptions, record: dict[str, Any] | maintenance_policies.PolicyRecord, *, table_uuid: str, uri: str, at: str
) -> None:
    """Hold ``at`` as the cadence stamp of the dataset ``uri`` under the policy ``record``, whatever ``at`` spells: a
    stamp maintenance's own writer would refuse to write."""
    stamp = maintenance_policies.CadenceStamp.under(policy(record), datetime.now(UTC)).model_dump(mode="json")
    put_record(control_root, storage_options, maintenance_policies.cadence_key(table_uuid, uri), {**stamp, "last_maintained_at": at})


def put_trash(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Hold ``record`` as a trash record under the uuid it names, whatever it holds: one no drop would file, such as a
    record naming no uuid, is filed under the empty one."""
    key = trash.key(str(record.get("uuid") or ""), kind="namespace" if record.get("kind") == "namespace" else "table")
    put_record(control_root, storage_options, key, record)


def read_due(control_root: str, storage_options: StorageOptions, table_uuid: str) -> DueRecord | None:
    """The due record maintenance holds for the table ``table_uuid``, or ``None``."""
    name = maintenance_due.due_key(table_uuid).rsplit("/", 1)[-1]
    return maintenance_due.read_due_named(control_root, storage_options, name)
