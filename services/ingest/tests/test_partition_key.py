"""Partitioning by KEY — the owner's "volume level for IIIF, folder level for S3".

The answer is a COLUMN, not a fragment boundary, and that is measured rather than preferred:

* Lance has no table-level partitioning — `lance_docs/ns_catalog/partitioning-spec.md:4` says so
  outright ("Lance tables do not natively support partitioning, instead promoting clustering") and
  `write_fragments` takes no partition key.
* Key-pure FRAGMENTS do not survive. Five fragments each holding one distinct key, committed, then
  `services/maintenance`'s default `compact_files()` → ONE fragment holding all five. Compaction is
  opt-OUT and sweeps every discovered dataset, so the merge is the default behaviour.
* A column plus a scalar index DOES survive that same compaction, answering an equality filter from
  the index rather than a scan.

So these tests pin the column and its PROVENANCE (the adapter, never the worker), not a layout.
"""

from __future__ import annotations

import pytest

from ingest.sources import SourceSpec, partition_key_for


# ── the key comes from the ADAPTER, and each kind defines its own ─────────────


def test_the_S3_partition_is_the_objects_FOLDER_not_the_runs_prefix() -> None:
    """ "Partition on folder level" — and the folder must VARY, or the column is worthless.

    Deriving it from `options.prefix` would give every unit in the run the same value, because the
    prefix is the run's ROOT. The containing folder of each object is the thing that actually
    differs, which is what makes the column worth indexing.
    """
    from ingest.adapters import register_builtin_sources

    register_builtin_sources()
    spec = SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "b", "prefix": "volumes/"})

    assert partition_key_for(spec, "s3://b/volumes/A0068688/0001.tif") == "s3://b/volumes/A0068688"
    assert partition_key_for(spec, "s3://b/volumes/A0099999/0001.tif") == "s3://b/volumes/A0099999"
    # Two objects in ONE folder share a key; that is the grouping the owner asked for.
    assert partition_key_for(spec, "s3://b/volumes/A0068688/0002.tif") == partition_key_for(spec, "s3://b/volumes/A0068688/0001.tif")


def test_an_object_at_the_bucket_ROOT_has_no_folder_and_gets_a_NULL() -> None:
    """A null is the honest answer. Inventing "/" or the bucket name would create a partition that
    does not exist and quietly group unrelated rows under it."""
    from ingest.adapters import register_builtin_sources

    register_builtin_sources()
    spec = SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "b"})

    assert partition_key_for(spec, "root-object.tif") is None


def test_an_UNREGISTERED_partition_rule_is_a_null_not_a_crash() -> None:
    """This runs on the WRITE path, after the bytes are already fetched.

    A missing grouping rule must not fail a unit that cost a network round trip — the label is
    metadata, and losing it is strictly better than losing the row.
    """
    assert partition_key_for(SourceSpec(kind="does-not-exist", project="p", dataset="d"), "any") is None


# ── the column reaches the table, positionally ───────────────────────────────


def test_units_to_table_writes_the_partition_column() -> None:
    from ingest.worker import units_to_table

    table = units_to_table(
        [("s3://b/vol-a/1.tif", b"a"), ("s3://b/vol-b/1.tif", b"b")],
        ["s3://b/vol-a", "s3://b/vol-b"],
    )

    assert table.column("partition_key").to_pylist() == ["s3://b/vol-a", "s3://b/vol-b"]


def test_omitting_partitions_writes_NULLS_rather_than_refusing() -> None:
    """Optional by design: a source with no meaningful grouping writes nulls, and every existing
    caller (including the tests written before this column) keeps working unchanged."""
    from ingest.worker import units_to_table

    table = units_to_table([("file:///a.tif", b"a")])

    assert table.column("partition_key").to_pylist() == [None]


def test_a_LENGTH_MISMATCH_is_refused_rather_than_misattributed() -> None:
    """Positional parallelism is the contract. Silently zipping to the shorter list would attribute
    rows to the WRONG partition — a wrong label is worse than no label, because it is believed."""
    from ingest.worker import units_to_table

    with pytest.raises(ValueError, match="partitions"):
        units_to_table([("a", b"1"), ("b", b"2")], ["only-one"])


# ── the value travels ADAPTER -> task -> row, never derived at the worker ────


def test_the_worker_never_DERIVES_the_partition_from_the_key() -> None:
    """The weld this design exists to prevent.

    The worker resolves units by URI SCHEME and knows nothing about volumes or prefixes. If it
    learned to parse `s3://b/folder/x.tif` into a folder, every future source's key convention would
    become the worker's business — which is exactly the coupling I1 removed from the backend.

    Asserted structurally: `units_to_table` given NO partitions must write nulls even for keys that
    obviously contain a folder. A worker that had started deriving would return the folder here.
    """
    from ingest.worker import units_to_table

    table = units_to_table([("s3://bucket/clearly/a/folder/file.tif", b"x")])

    assert table.column("partition_key").to_pylist() == [None], "the worker derived a partition key from the URI — that belongs to the adapter"


def test_the_task_carries_the_key_so_the_drain_needs_no_source_knowledge() -> None:
    from ingest.queue import UnitTask

    task = UnitTask(run_id="r", chunk_id="c", key="s3://b/f/x.tif", dataset_uri="/tmp/d.lance", partition_key="s3://b/f")

    assert task.partition_key == "s3://b/f"
    # Optional, so a task published by an older build still validates rather than failing the run.
    assert UnitTask(run_id="r", chunk_id="c", key="k", dataset_uri="/tmp/d.lance").partition_key is None
