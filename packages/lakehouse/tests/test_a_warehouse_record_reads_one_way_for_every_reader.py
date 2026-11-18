"""The warehouse registry as its readers outside the catalog read it (`lakehouse.warehouse_records`).

One model for the record: what is no warehouse record refuses the read a decision is made from, and a record is
quarantined by the one status that says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lakehouse import warehouse_records
from lakehouse.storage_options import StorageOptions


def _filed(root: Path, name: str, record: object) -> None:
    registry = root / warehouse_records.REGISTRY_PREFIX
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{name}.json").write_text(json.dumps(record))


def test_what_is_no_warehouse_record_refuses_the_whole_read_naming_it_and_its_field(tmp_path: Path) -> None:
    _filed(tmp_path, "wh-a", {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"})
    # Names a bucket and no project: whose it is, and so whether it is governed, is unknown.
    _filed(tmp_path, "wh-b", {"id": "wh-b", "bucket": "bkt-b", "root_uri": "s3://bkt-b"})
    root, options = str(tmp_path), StorageOptions()

    with pytest.raises(warehouse_records.UnreadableWarehouseRecordError, match=r"naming 'wh-b'.*project is not what a record holds"):
        warehouse_records.read_warehouse_records(root, options)


@pytest.mark.parametrize(("status", "quarantined"), [(None, False), ("active", False), ("deactivated", True)], ids=["a-null-status", "active", "deactivated"])
def test_a_warehouse_is_quarantined_only_by_a_status_that_says_so(tmp_path: Path, status: str | None, quarantined: bool) -> None:
    _filed(tmp_path, "wh-a", {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "status": status})
    root, options = str(tmp_path), StorageOptions()

    record = warehouse_records.warehouse_record(root, options, "wh-a")
    maintained = warehouse_records.maintainable_buckets(warehouse_records.read_warehouse_records(root, options))

    assert record is not None and (record.is_deactivated, maintained) == (quarantined, [] if quarantined else ["bkt-a"])


@pytest.mark.parametrize("status", ["paused", "ACTIVE", "Deactivated"])
def test_a_status_no_door_writes_is_no_status_any_reader_guesses_at(tmp_path: Path, status: str) -> None:
    """A word that is neither status would quarantine a tenant for one reader and serve it for another, so the record
    is unreadable for all of them, read by its key or in the whole read."""
    _filed(tmp_path, "wh-a", {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "status": status})
    root, options = str(tmp_path), StorageOptions()

    with pytest.raises(warehouse_records.UnreadableWarehouseRecordError, match="status"):
        warehouse_records.warehouse_record(root, options, "wh-a")
    with pytest.raises(warehouse_records.UnreadableWarehouseRecordError, match="status"):
        warehouse_records.read_warehouse_records(root, options)
