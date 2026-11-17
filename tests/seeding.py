"""Control-root records a test puts in place before the door it drives runs, and one it reads back.

Each write is whole and unconditional, which no door's is: a door creates a record on its absence and changes it on
its ETag, so these stand for the state an earlier request left, never for a request.
"""

from __future__ import annotations

from typing import Any

from lakehouse import control_prefixes, maintenance_due, maintenance_policies
from lakehouse.maintenance_due import DueRecord
from lakehouse.record_store import put_record, record_key
from lakehouse.storage_options import StorageOptions


def put_warehouse(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Hold ``record`` as the warehouse ``record["id"]``'s registry record."""
    put_record(control_root, storage_options, f"{control_prefixes.WAREHOUSES}/{record['id']}.json", record)


def put_project(control_root: str, storage_options: StorageOptions, record: dict[str, str]) -> None:
    """Hold ``record`` as the project ``record["id"]``'s registry record."""
    put_record(control_root, storage_options, f"{control_prefixes.PROJECTS}/{record['id']}.json", record)


def put_policy(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
    """Hold ``record`` as the maintenance policy of the object it names, under the key the policy door writes."""
    key = record_key(maintenance_policies.PREFIX, record["kind"], maintenance_policies.policy_key(record))
    put_record(control_root, storage_options, key, record)


def read_due(control_root: str, storage_options: StorageOptions, table_uuid: str) -> DueRecord | None:
    """The due record maintenance holds for the table ``table_uuid``, or ``None``."""
    name = maintenance_due.due_key(table_uuid).rsplit("/", 1)[-1]
    return maintenance_due.read_due_named(control_root, storage_options, name)
