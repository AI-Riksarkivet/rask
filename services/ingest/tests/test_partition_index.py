"""#43 — the `partition_key` column pays off only with an index, and nothing created one.

The column is deliberate and measured (`runtime.py:97-121`): Lance has NO table partitioning
(`lance_docs/ns_catalog/partitioning-spec.md:4` — "Lance tables do not natively support partitioning,
instead promoting clustering"), and key-pure FRAGMENTS do not survive — five fragments holding one
distinct key each, committed, then `compact_files()` merged them into ONE, and compaction is opt-OUT.

The same measurement records what DOES survive: "a BITMAP index on the column made
`partition_key = 'x'` answer as an index-served ScalarIndexQuery rather than a scan."

That half did not exist. `services/maintenance` cannot supply it — `optimize_indices()` folds
compacted fragments into indices that ALREADY exist, and `create_scalar_index` appears nowhere in
that service. So every partition query was a full scan, forever, on a column built for the purpose.
"""

from __future__ import annotations

import lance
import pyarrow as pa
import pytest

from ingest.lander import PARTITION_INDEX, PARTITION_INDEX_NAME, _ensure_partition_index


@pytest.fixture
def dataset(tmp_path):  # type: ignore[no-untyped-def]
    """A bronze-shaped dataset with two partitions — enough for an equality filter to have a choice."""
    table = pa.table(
        {
            "key": [f"s3://b/p/{i}.tif" for i in range(6)],
            "partition_key": ["vol-a", "vol-a", "vol-a", "vol-b", "vol-b", "vol-b"],
        }
    )
    return lance.write_dataset(table, str(tmp_path / "bronze.lance"), data_storage_version="2.2", enable_stable_row_ids=True)


def test_the_index_is_CREATED_because_nothing_else_creates_it(dataset: lance.LanceDataset) -> None:
    """`services/maintenance` MAINTAINS indices; it never creates any. Verified by grep over that whole
    service — `create_scalar_index` does not appear. A dataset arriving there without an index gets
    nothing from it, so the creating moment has to be the writer's own commit."""
    assert dataset.describe_indices() == [], "sanity: a fresh dataset has no index"

    _ensure_partition_index(dataset)

    names = [i.name for i in dataset.describe_indices()]
    assert PARTITION_INDEX_NAME in names, f"no partition index was created; found {names}"


def test_it_is_a_BITMAP_because_the_column_is_low_cardinality_equality(dataset: lance.LanceDataset) -> None:
    """One distinct value per IIIF volume or S3 folder, queried as `partition_key = 'x'`. Lance stores a
    compressed Roaring Bitmap per distinct value (`guide.md:3211-3215`); BTREE serves ranges, which this
    column never has."""
    _ensure_partition_index(dataset)

    index = next(i for i in dataset.describe_indices() if i.name == PARTITION_INDEX_NAME)

    # `type_url` is the protobuf descriptor — `/lance.table.BitmapIndexDetails` for a BITMAP.
    assert PARTITION_INDEX.lower() in index.type_url.lower(), f"expected a {PARTITION_INDEX} index, got {index.type_url}"


def test_an_equality_filter_is_INDEX_SERVED_not_a_scan(dataset: lance.LanceDataset) -> None:
    """The property the whole column exists for, asserted against the PLAN rather than the result.

    A filter returns the right rows either way — scanning six of them is indistinguishable from an
    index lookup by output alone, which is exactly how this could have shipped looking correct. The
    plan is the only place the difference is visible.
    """
    _ensure_partition_index(dataset)

    plan = dataset.scanner(filter="partition_key = 'vol-a'", prefilter=True).explain_plan(verbose=True)

    assert "ScalarIndexQuery" in plan or "MaterializeIndex" in plan, f"the filter fell back to a scan; plan was:\n{plan}"


def test_running_it_TWICE_is_a_no_op(dataset: lance.LanceDataset) -> None:
    """It runs on EVERY commit, and a dataset takes many. Rebuilding the index each time would make a
    routine append progressively more expensive as the tier grows — the opposite of what the index is
    for."""
    _ensure_partition_index(dataset)
    before = len(dataset.describe_indices())

    _ensure_partition_index(dataset)

    assert len(dataset.describe_indices()) == before, "the second call built a duplicate index"


def test_it_NEVER_fails_a_commit_that_already_landed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A run that committed its data must not fail because an optimisation could not be built.

    Same reasoning as I8 for lineage, and it costs nothing: the index is recoverable at any later
    commit, whereas a raise here would turn a landed bronze write into a failed run.
    """

    class _Broken:
        def describe_indices(self) -> list[object]:
            raise RuntimeError("index catalogue unreadable")

    _ensure_partition_index(_Broken())  # must not raise


def test_the_CATALOG_COMMIT_path_gets_the_same_indexes(tmp_path) -> None:
    """The deployed path bypasses the lander (the catalog folds fragments server-side), and every
    catalog-committed table shipped with NO indexes — found by the goal's explain gate against a
    table the live pipeline had just written (`describe_indices() == []`, 2026-08-07). One policy,
    two entry points: `ensure_indexes_at(uri)` must produce exactly what the lander's own commit
    produces."""
    import lance

    from ingest.lander import CREATION_FLAGS, ID_INDEX_NAME, PARTITION_INDEX_NAME, ensure_indexes_at
    from ingest.worker import units_to_table

    uri = str(tmp_path / "t.lance")
    lance.write_dataset(units_to_table([("file:///a.tif", b"II*\x00a")]), uri, **CREATION_FLAGS)

    ensure_indexes_at(uri)

    names = {getattr(i, "name", i.get("name") if isinstance(i, dict) else "") for i in lance.dataset(uri).describe_indices()}
    assert PARTITION_INDEX_NAME in names
    assert ID_INDEX_NAME in names
