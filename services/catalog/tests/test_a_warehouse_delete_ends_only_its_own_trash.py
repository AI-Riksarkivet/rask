"""A warehouse delete ends the trash that is its own and no other warehouse's ([[LH-495]], LH-102).

A recoverable drop keeps a record, the object's grants, its policy and its location's claim for an undrop. Deleting the
warehouse ends them, so it must pick out exactly its own: the records whose name begins with the warehouse's id
([[LH-593]]) and that its own creation filed (ADR 0245, review T2). One project may back two warehouses with one bucket,
so a record in the same bucket under the other warehouse's name, one under a warehouse whose id merely begins with the
same letters, and one an earlier creation of the warehouse filed under its id, is never ended by this delete.

Driven over a real local control root with the trash records the doors write.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from lance_namespace import InternalError

from catalog.services import warehouses
from lakehouse import trash
from lakehouse.storage_options import StorageOptions


def _warehouse(control: str, warehouse_id: str) -> dict[str, str]:
    """A live record of ``warehouse_id``, a new creation of it each call."""
    record = {"id": warehouse_id, "bucket": "acme-bkt", "root_uri": "s3://acme-bkt", "project": "acme"}
    warehouses.put_warehouse(control, StorageOptions(), record)
    return warehouses.get_warehouse_record(control, StorageOptions(), warehouse_id) or {}


def _trash(control: str, canonical_id: str, location: str = "", *, kind: trash.TrashKind = "table") -> None:
    trash.put(
        control,
        StorageOptions(),
        trash.make_record(canonical_id, uuid=str(uuid4()), location=location, dropped_by="tester", grace_days=7, kind=kind),
    )


def test_the_trash_a_warehouse_ends_is_its_own_and_each_child_comes_before_its_parent(tmp_path: Path) -> None:
    control = str(tmp_path / "control")
    for sibling in ("acme-gold", "acme-workshop"):
        _warehouse(control, sibling)
    _warehouse(control, "acme-work")
    _trash(control, "acme-work$earlier$t", "s3://acme-bkt/g_earlier$t")
    work = _warehouse(control, "acme-work")
    _trash(control, "acme-work$bronze$trashed", "s3://acme-bkt/a_bronze$trashed")
    _trash(control, "acme-work$silver", kind="namespace")
    _trash(control, "acme-work$silver$inner", kind="namespace")
    _trash(control, "acme-work$silver$inner$s", "s3://acme-bkt/b_silver$inner$s")
    _trash(control, "acme-work$silver$declared")
    _trash(control, "acme-gold$gold$g", "s3://acme-bkt/d_gold$g")
    _trash(control, "acme-gold$platinum", kind="namespace")
    _trash(control, "acme-workshop$p$t", "s3://acme-bkt/e_p$t")
    _trash(control, "lance_catalog$beta$t", "s3://estate-bkt/f_beta$t")

    owned = warehouses.trash_owned_by(control, StorageOptions(), work)

    ended = [(str(record["kind"]), str(record["id"])) for record in owned]
    assert sorted(ended) == [
        ("namespace", "acme-work$silver"),
        ("namespace", "acme-work$silver$inner"),
        ("table", "acme-work$bronze$trashed"),
        ("table", "acme-work$silver$declared"),
        ("table", "acme-work$silver$inner$s"),
    ]
    assert [name for kind, name in ended if kind == "namespace"] == ["acme-work$silver$inner", "acme-work$silver"]
    assert [kind for kind, _ in ended] == sorted((kind for kind, _ in ended), key=lambda kind: kind == "namespace"), "a namespace came before a table"


@pytest.mark.parametrize(
    ("written", "refusal"),
    [
        pytest.param("{not json", "reading the trash failed", id="unreadable"),
        pytest.param('{"id": "acme-work$bronze$unstamped", "kind": "table", "uuid": "u1"}', "name no creation of warehouse", id="naming-no-creation"),
    ],
)
def test_a_trash_record_the_selection_cannot_account_for_refuses_it(tmp_path: Path, written: str, refusal: str) -> None:
    """A record that cannot be read, or one under the warehouse's name naming no creation of it (ADR 0245, second review
    S4), may be the warehouse's own ([[LH-572]]): the selection refuses rather than skipping it, and the delete with it
    rather than ending the rest and leaving it holding its grants and its claim for a warehouse that no longer exists."""
    control = str(tmp_path / "control")
    work = _warehouse(control, "acme-work")
    _trash(control, "acme-work$bronze$trashed", "s3://acme-bkt/a_bronze$trashed")
    (tmp_path / "control" / "_trash" / "written.json").write_text(written)

    with pytest.raises(InternalError, match=refusal):
        warehouses.trash_owned_by(control, StorageOptions(), work)
