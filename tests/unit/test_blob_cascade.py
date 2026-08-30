"""§9 P3 plumbing — a blob-v2 (media) column survives a medallion cascade hop.

``transform_stage``'s old ``to_table()`` demoted a blob column to its descriptions struct (tagged with
the legacy encoding key), which the 2.2 write then rejected. These tests pin the fix: a blob column is
carried through intact (``read_blobs`` + ``blob_array``) while a plain stage keeps working.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
from lance import blob_array, blob_field

from medallion.services.compute import transform_stage


def _write(uri: str, table: pa.Table) -> None:
    lance.write_dataset(table, uri, mode="overwrite", data_storage_version="2.2")


def test_blob_column_survives_a_cascade_hop(tmp_path: Path) -> None:
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("src", pa.string())])
    _write(
        bronze,
        pa.table(
            {"id": [1, 2], "payload": blob_array([b"img-a", b"video" * 1000]), "src": ["cam-a", "cam-b"]},
            schema=schema,
        ),
    )

    result = transform_stage(bronze, silver, {}, stage="silver")

    ds = lance.dataset(silver)
    assert ds.data_storage_version == "2.2"
    assert result.row_count == 2
    # the blob is intact and re-typed as blob-v2 (not the legacy descriptions struct)
    assert ds.schema.field("payload").type.extension_name == "lance.blob.v2"
    assert ds.read_blobs("payload", indices=[0])[0][1] == b"img-a"
    # non-blob columns + the fresh stage stamp carried through
    assert ds.to_table(columns=["src"]).column("src").to_pylist() == ["cam-a", "cam-b"]
    assert ds.to_table(columns=["stage"]).column("stage").to_pylist() == ["silver", "silver"]
    # the measured WriteResult captures a blob-aware schema facet for the WROTE-edge lineage (P4)
    assert {"name": "payload", "type": "blob"} in result.fields
    assert {"name": "src", "type": "string"} in result.fields


def test_stage_restamped_not_duplicated_when_carrying_a_blob(tmp_path: Path) -> None:
    bronze, silver = str(tmp_path / "b"), str(tmp_path / "s")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("stage", pa.string())])
    _write(bronze, pa.table({"id": [1], "payload": blob_array([b"x"]), "stage": ["bronze"]}, schema=schema))

    transform_stage(bronze, silver, {}, stage="silver")

    ds = lance.dataset(silver)
    assert ds.to_table(columns=["stage"]).column("stage").to_pylist() == ["silver"]  # replaced, not doubled
    assert "stage" in ds.schema.names and ds.schema.names.count("stage") == 1


def test_plain_stage_still_stamps_without_blob(tmp_path: Path) -> None:
    src, dst = str(tmp_path / "src"), str(tmp_path / "dst")
    _write(src, pa.table({"id": [1, 2, 3]}))

    transform_stage(src, dst, {}, stage="bronze")

    ds = lance.dataset(dst)
    assert ds.count_rows() == 3
    assert ds.to_table(columns=["stage"]).column("stage").to_pylist() == ["bronze"] * 3


def test_carry_forward_preserves_fixed_size_list_alongside_a_blob(tmp_path: Path) -> None:
    # a blob column AND a FixedSizeList embedding both survive the blob-rebuild path, row-aligned
    bronze, silver = str(tmp_path / "b"), str(tmp_path / "s")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("embedding", pa.list_(pa.float32(), 4))])
    _write(
        bronze,
        pa.table(
            {
                "id": [1, 2],
                "payload": blob_array([b"a", b"b"]),
                # float32-exact (dyadic) values so the round-trip compares equal without rounding noise
                "embedding": pa.array([[0.5, 0.25, 0.75, 1.0], [0.125, 0.375, 0.625, 0.875]], pa.list_(pa.float32(), 4)),
            },
            schema=schema,
        ),
    )

    transform_stage(bronze, silver, {}, stage="gold")

    ds = lance.dataset(silver)
    assert ds.schema.field("payload").type.extension_name == "lance.blob.v2"
    assert ds.schema.field("embedding").type == pa.list_(pa.float32(), 4)
    assert ds.read_blobs("payload", indices=[1])[0][1] == b"b"
    assert ds.to_table(columns=["embedding"]).column("embedding")[1].as_py() == [0.125, 0.375, 0.625, 0.875]


def test_carry_forward_handles_multiple_blob_columns(tmp_path: Path) -> None:
    bronze, silver = str(tmp_path / "b"), str(tmp_path / "s")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("img"), blob_field("audio")])
    _write(
        bronze,
        pa.table({"id": [1], "img": blob_array([b"pixels"]), "audio": blob_array([b"waveform"])}, schema=schema),
    )

    transform_stage(bronze, silver, {}, stage="silver")

    ds = lance.dataset(silver)
    assert ds.read_blobs("img", indices=[0])[0][1] == b"pixels"
    assert ds.read_blobs("audio", indices=[0])[0][1] == b"waveform"


def test_carry_forward_handles_zero_row_blob_dataset(tmp_path: Path) -> None:
    bronze, silver = str(tmp_path / "b"), str(tmp_path / "s")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload")])
    _write(bronze, pa.table({"id": pa.array([], pa.int64()), "payload": blob_array([])}, schema=schema))

    transform_stage(bronze, silver, {}, stage="silver")

    ds = lance.dataset(silver)
    assert ds.count_rows() == 0
    assert ds.schema.field("payload").type.extension_name == "lance.blob.v2"
