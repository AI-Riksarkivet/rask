"""A warehouse delete ends the trash that is its own and no other warehouse's ([[LH-495]], LH-102).

A recoverable drop keeps a record, the object's grants, its policy and its location's claim for an undrop. Deleting the
warehouse ends them, so it must pick out exactly its own: the records under a namespace bound to it, under a trashed
namespace whose recorded binding names it, and the tables whose bytes sit in its bucket after their namespace was
dropped. One project may back two warehouses with one bucket, so a record in the same bucket under the other
warehouse's namespace, live or trashed, is the other's and is never ended by this delete.

Driven over a real local control root with the trash and binding records the doors write.
"""

from __future__ import annotations

from pathlib import Path

from catalog.services import warehouses
from service_kit.lakehouse import trash


def _trash(control: str, canonical_id: str, location: str = "", *, kind: str = "table", warehouse: str | None = None) -> None:
    binding = {"warehouse_id": warehouse, "root_uri": "s3://acme-bkt"} if warehouse else None
    trash.put(control, {}, trash.make_record(canonical_id, location=location, dropped_by="tester", grace_days=7, kind=kind, binding=binding))


def test_the_trash_a_warehouse_ends_is_its_own_and_each_child_comes_before_its_parent(tmp_path: Path) -> None:
    control = str(tmp_path / "control")
    warehouses.bind_namespace(control, {}, "bronze", "acme-work", "s3://acme-bkt")
    warehouses.bind_namespace(control, {}, "gold", "acme-gold", "s3://acme-bkt")
    _trash(control, "bronze$trashed", "s3://acme-bkt/a_bronze$trashed")
    _trash(control, "silver", kind="namespace", warehouse="acme-work")
    _trash(control, "silver$inner", kind="namespace")
    _trash(control, "silver$inner$s", "s3://acme-bkt/b_silver$inner$s")
    _trash(control, "silver$declared")
    _trash(control, "dropped$t", "s3://acme-bkt/c_dropped$t")
    _trash(control, "gold$g", "s3://acme-bkt/d_gold$g")
    _trash(control, "platinum", kind="namespace", warehouse="acme-gold")
    _trash(control, "platinum$p", "s3://acme-bkt/e_platinum$p")
    _trash(control, "beta$t", "s3://beta-bkt/f_beta$t")

    owned = warehouses.trash_owned_by(control, {}, "acme-work", bucket="acme-bkt", delimiter="$")

    ended = [(str(record["kind"]), str(record["id"])) for record in owned]
    assert sorted(ended) == [
        ("namespace", "silver"),
        ("namespace", "silver$inner"),
        ("table", "bronze$trashed"),
        ("table", "dropped$t"),
        ("table", "silver$declared"),
        ("table", "silver$inner$s"),
    ]
    assert [name for kind, name in ended if kind == "namespace"] == ["silver$inner", "silver"]
    assert [kind for kind, _ in ended] == sorted((kind for kind, _ in ended), key=lambda kind: kind == "namespace"), "a namespace came before a table"
