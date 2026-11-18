"""Maintenance maintains a warehouse only while it is live (ADR 0245, second review S4).

A warehouse being created holds nothing yet, and one being deleted is emptied by its own delete, which ends its trash and
purges its bytes under the claim it holds; compacting or reclaiming beneath either would race the catalog's own work. The
registry reader maintenance decides by (`lakehouse.warehouse_records`) reads a record's state, as the catalog's resolver
does, so neither the bucket set the sweep and the purge build on nor a table's own warehouse names one that is not live.

Driven over a real local control root holding the records the catalog writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lakehouse import warehouse_records
from lakehouse.storage_options import StorageOptions


@pytest.mark.parametrize(("state", "maintained"), [("live", True), ("creating", False), ("deleting", False)])
def test_a_warehouse_is_maintained_only_while_it_is_live(tmp_path: Path, state: str, maintained: bool) -> None:
    registry = tmp_path / warehouse_records.REGISTRY_PREFIX
    registry.mkdir(parents=True)
    record = {"id": "wh-a", "bucket": "wh-a-bucket", "project": "acme", "uuid": "1" * 32, "project_uuid": "2" * 32, "state": state}
    (registry / "wh-a.json").write_text(json.dumps(record))
    control = str(tmp_path)

    buckets = warehouse_records.maintainable_buckets(warehouse_records.read_warehouse_records(control, StorageOptions()))
    named = warehouse_records.warehouse_record(control, StorageOptions(), "wh-a")

    assert (buckets == ["wh-a-bucket"], named is not None) == (maintained, maintained)
