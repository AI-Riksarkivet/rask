"""The lakehouse control-plane kernel drain — SKG-08.

A finding against `lakehouse` about a seam written more than once:

* **SKG-08** — four hand-rolled object-store record registries (`protection`, `maintenance_policies`,
  `trash`, `warehouse_records`) each carrying their own byte-identical hashed-key helper and their own
  list-with-broad-except body. The copies had already DRIFTED: `get_policy` grew no `isinstance(loaded,
  dict)` guard while both its twins have one, so a malformed policy record propagates into
  `resolve_policy` as whatever JSON happened to be on disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from lakehouse.records import maintenance_policies, project_records, trash, warehouse_records
from lakehouse.records.base_registry import BaseRegistry, UnreadableBaseRecordError, read_base_record, record_key_for
from lakehouse.records.maintenance_policies import MalformedPolicyError, get_policy
from lakehouse.records.project_records import UnreadableProjectRecordError
from lakehouse.records.record_store import UnreadableRecordError
from lakehouse.records.trash import UnreadableTrashRecordError
from lakehouse.records.warehouse_records import UnreadableWarehouseRecordError
from lakehouse.storage.options import StorageOptions
from tests import seeding


_TABLE_UUID = "7c0e2f4a-5b1d-4e8f-9a3c-2d6b8e1f0a47"
_NAMESPACE_UUID = "3f9d1c2e-8a4b-4c6d-b5e7-1a2b3c4d5e6f"


# --------------------------------------------------------------------------------------------------
# SKG-08 — one registry primitive, and the drift the four copies produced
# --------------------------------------------------------------------------------------------------


def test_a_policy_record_that_is_no_object_is_refused_by_its_key_and_never_read_as_unset(tmp_path: Path) -> None:
    """A stored record that is not an object may be a hold, so the read refuses it by the key it is stored under, as the
    trash's and the protection's reads refuse theirs."""
    seeding.put_policy(str(tmp_path), StorageOptions(), {"kind": "table", "id": "db$t", "uuid": _TABLE_UUID})
    key = next(p for p in (tmp_path / "_policies").iterdir() if p.suffix == ".json")
    key.write_text(json.dumps(["not", "a", "record"]))

    with pytest.raises(MalformedPolicyError, match=key.name):
        get_policy(str(tmp_path), StorageOptions(), "table", key=_TABLE_UUID)


def test_the_record_key_shape_is_unchanged_by_the_de_duplication() -> None:
    """A golden key per prefix: records already on the control root must stay addressable.

    Protection and policy records are keyed by their object's uuid (ADR 0173 §8), the trash record by the
    dropped object's name; the derivation from that key is the one ``record_key``.
    """
    from lakehouse.records.record_store import record_key

    assert record_key("_protection", "table", _TABLE_UUID) == "_protection/table-9f9a37202eeb7dc7cabba84b.json"
    assert record_key("_policies", "namespace", _NAMESPACE_UUID) == "_policies/namespace-3c0b296fe7c2d32e9edd2812.json"
    assert record_key("_trash", "table", "db$t") == "_trash/table-f74c29b36c7f71a511912ddc.json"


def _a_base_record(root: Path) -> object:
    location = "s3://bucket/t"
    stored = root / record_key_for(location)
    stored.parent.mkdir(parents=True)
    stored.write_text(json.dumps({"entries": "not a list"}))
    return read_base_record(BaseRegistry(control_root=str(root), storage_options=StorageOptions()), location)


def _a_policy_record(_root: Path) -> object:
    return maintenance_policies.read({"kind": "table"}, at="_policies/table-x.json")


def _a_project_record(_root: Path) -> object:
    return project_records.read({"status": 7}, at="_projects/p.json")


def _a_warehouse_record(_root: Path) -> object:
    return warehouse_records.read({"id": "w"}, at="_warehouses/w.json")


def _a_trash_record(_root: Path) -> object:
    return trash.read({"id": 7}, at="_trash/table-x.json")


@pytest.mark.parametrize(
    ("reader", "refusal"),
    [
        pytest.param(_a_base_record, UnreadableBaseRecordError, id="base"),
        pytest.param(_a_policy_record, MalformedPolicyError, id="policy"),
        pytest.param(_a_project_record, UnreadableProjectRecordError, id="project"),
        pytest.param(_a_warehouse_record, UnreadableWarehouseRecordError, id="warehouse"),
        pytest.param(_a_trash_record, UnreadableTrashRecordError, id="trash"),
    ],
)
def test_every_registry_refuses_a_record_it_cannot_read_as_its_own_unreadable_record_error(
    tmp_path: Path, reader: Callable[[Path], object], refusal: type[UnreadableRecordError]
) -> None:
    """One family for a stored record no reader can parse, a ``ValueError`` by its kind: invalid input to the reader."""
    with pytest.raises(UnreadableRecordError) as caught:
        reader(tmp_path)

    assert type(caught.value) is refusal
    assert isinstance(caught.value, ValueError)
