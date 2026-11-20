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
from pathlib import Path

import pytest

from lakehouse.records.maintenance_policies import MalformedPolicyError, get_policy
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
        get_policy(str(tmp_path), StorageOptions(), "table", _TABLE_UUID)


def test_the_record_key_shape_is_unchanged_by_the_de_duplication() -> None:
    """A golden key per prefix: records already on the control root must stay addressable.

    Protection and policy records are keyed by their object's uuid (ADR 0173 §8), the trash record by the
    dropped object's name; the derivation from that key is the one ``record_key``.
    """
    from lakehouse.records.record_store import record_key

    assert record_key("_protection", "table", _TABLE_UUID) == "_protection/table-9f9a37202eeb7dc7cabba84b.json"
    assert record_key("_policies", "namespace", _NAMESPACE_UUID) == "_policies/namespace-3c0b296fe7c2d32e9edd2812.json"
    assert record_key("_trash", "table", "db$t") == "_trash/table-f74c29b36c7f71a511912ddc.json"
