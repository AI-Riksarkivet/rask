"""A re-seeded bronze keeps the row identity silver references.

THE ESTATE ALREADY DEPENDS ON THIS AND ALREADY SAYS SO. `ingest/catalog.py::A14` refuses any governed
dataset created without `enable_stable_row_ids`, because "every silver/gold row references bronze
through `source_rowid`" and setting the flag later is a silent no-op. The cascade duly sets it at
every create — and then wrote every tier `mode="overwrite"`, which is the one Lance operation that
re-mints every `_rowid`.

MEASURED against pylance 10.0.0: a `mode="overwrite"` re-write of a stable-row-id dataset moves
`_rowid` from `[0,1,2]` to `[3,4,5]`; `merge_insert("id")` leaves it `[0,1,2]`.

MEASURED on the deployed estate 2026-09-06, which is what makes this a defect rather than a
tidiness argument:

    bronze   8 rows, 20 versions, live _rowid [2004..2011]
    silver   source_rowid          [88..95]
    DANGLING 8 of 8

Every silver row pointed at a bronze row that no longer existed. The provenance chain owner ruling D1
makes mandatory for impact analysis resolved to nothing, and nothing anywhere reported it — a dangling
`source_rowid` has no symptom until someone asks the question it exists to answer.

The seed is IDEMPOTENT either way: `merge_insert` on `id` updates a row that is already there and
inserts one that is not, so a re-seed of the same rows is still a no-op in content. What changes is
that identity survives it.
"""

from __future__ import annotations

from pathlib import Path

import lance

from medallion.services.compute import seed_bronze


def _row_ids(uri: str) -> list[int]:
    return lance.dataset(uri).to_table(columns=[], with_row_id=True).column("_rowid").to_pylist()


def test_a_re_seed_preserves_the_row_ids_silver_references(tmp_path: Path) -> None:
    uri = str(tmp_path / "bronze.lance")

    seed_bronze(uri, {}, rows=8)
    first = _row_ids(uri)
    assert len(first) == 8

    # The cascade head re-seeds on every produce — this is the ordinary path, not a corner case.
    seed_bronze(uri, {}, rows=8)
    second = _row_ids(uri)

    assert second == first, (
        f"the re-seed re-minted every row id ({first} -> {second}), so every silver `source_rowid` "
        f"written against the first seed now names a row that does not exist"
    )


def test_a_re_seed_does_not_multiply_versions_without_changing_rows(tmp_path: Path) -> None:
    """20 versions of an 8-row table is what overwrite-per-run produces, and the 7-day GC then spends
    its time reclaiming copies of a dataset that never changed."""
    uri = str(tmp_path / "bronze.lance")
    seed_bronze(uri, {}, rows=8)
    for _ in range(4):
        seed_bronze(uri, {}, rows=8)
    rows = lance.dataset(uri).count_rows()
    assert rows == 8, f"five identical seeds produced {rows} rows — the merge is not converging on `id`"


def test_a_re_seed_with_more_rows_keeps_the_originals_identity(tmp_path: Path) -> None:
    """Growth must not re-mint what was already there: an incremental ingest is the normal case, and
    it is precisely when a stale `source_rowid` would go unnoticed."""
    uri = str(tmp_path / "bronze.lance")
    seed_bronze(uri, {}, rows=4)
    before = dict(zip(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist(), _row_ids(uri), strict=True))

    seed_bronze(uri, {}, rows=8)
    after = dict(zip(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist(), _row_ids(uri), strict=True))

    kept = {k: v for k, v in before.items() if after.get(k) == v}
    assert len(kept) == len(before), f"growing the seed re-minted ids for rows that already existed: {before} -> {after}"
