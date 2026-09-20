"""Deleting a warehouse clears the trash records that pointed into it.

[[LH-102]]. A dropped table leaves a TRASH record naming its location so it can be undropped inside
the retention window. Deleting the warehouse removes the registry record that made that location
maintainable — and the trash record survived it, naming a root the estate no longer maintains.

MEASURED ON THE LIVE ESTATE 2026-09-20, which is how this was found. A full non-destructive census
of the expired queue read `due=877 would_purge=8 refused=869`, and every one of the 869 was refused
`location … is outside the maintained estate`, clustered into 45 distinct `tracka<hex>-wh` buckets at
~26 tables each. Those are e2e fixture warehouses whose suite DOES clean up after itself — it drops
each table and then deletes the warehouse with `purge_bucket=true`. The residue is not a missing
cleanup; it is this ordering, on every warehouse delete the estate has ever done.

THE CLUTTER IS THE SMALLER HALF. A trash record promises recoverability, and `purge_bucket=true`
destroys the bytes it promises — so the record outlives what it describes and an undrop against it
can only succeed at naming something that is gone. Clearing the records with the warehouse keeps the
promise and the bytes in the same lifetime.

Scoped to the warehouse's own root and nothing else: a record under another warehouse is another
tenant's, and a purge that reached past its warehouse would be the destructive mistake this whole
area is careful about.
"""

from __future__ import annotations

from pathlib import Path

from catalog.services import warehouses
from service_kit.lakehouse import trash


def _record(canonical_id: str, location: str) -> dict[str, object]:
    return trash.make_record(canonical_id, location=location, dropped_by="tester", grace_days=7)


def test_a_warehouse_delete_clears_the_trash_that_pointed_into_it(tmp_path: Path) -> None:
    control_root = str(tmp_path / "control")
    trash.put(control_root, {}, _record("ns$doomed", "s3://acme-wh/abc_ns$doomed"))

    warehouses.delete_warehouse_record(control_root, {}, "acme-wh")

    assert trash.get(control_root, {}, "ns$doomed") is None, (
        "the trash record outlived the warehouse that gave its location meaning, so it names a root "
        "the estate no longer maintains and every purge tick will refuse it forever"
    )


def test_another_warehouses_trash_is_untouched(tmp_path: Path) -> None:
    """THE CONTROL, and the one that matters: reaching past the warehouse is the destructive mistake."""
    control_root = str(tmp_path / "control")
    trash.put(control_root, {}, _record("ns$doomed", "s3://acme-wh/abc_ns$doomed"))
    trash.put(control_root, {}, _record("other$keep", "s3://beta-wh/def_other$keep"))

    warehouses.delete_warehouse_record(control_root, {}, "acme-wh")

    assert trash.get(control_root, {}, "other$keep") is not None, "a delete reached into another warehouse's trash"


def test_a_warehouse_with_no_trash_deletes_cleanly(tmp_path: Path) -> None:
    """Idempotence: the record delete is already retry-safe and the trash sweep must not change that."""
    control_root = str(tmp_path / "control")

    warehouses.delete_warehouse_record(control_root, {}, "never-existed")
