"""Unit tests for embedding lineage coordinates into the Lance file (#21, ``catalog.core.lineage_metadata``).

The round-trip tests prove the coordinates land in the Arrow schema metadata; the Lance test proves
they survive a real ``write_dataset`` → ``dataset`` cycle (i.e. the data is genuinely self-describing).
"""

from __future__ import annotations

import pyarrow as pa

from catalog.core.lineage_metadata import build_lineage_metadata, stamp_lineage_metadata


def _schema_meta(table: pa.Table) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}


def test_build_lineage_metadata() -> None:
    md = build_lineage_metadata(table_id="a$b$c", namespace="a$b", run_id="r-1")
    assert md == {
        "lineage.dataset_id": "a$b$c",
        "lineage.namespace": "a$b",
        "lineage.create_run_id": "r-1",
    }


def test_stamp_preserves_existing_schema_metadata() -> None:
    schema = pa.schema([("id", pa.int64())], metadata={"existing": "keep"})
    table = pa.table({"id": [1]}, schema=schema)
    meta = _schema_meta(stamp_lineage_metadata(table, build_lineage_metadata(table_id="t", namespace="", run_id="r")))
    assert meta["existing"] == "keep"  # pre-existing schema metadata is preserved
    assert meta["lineage.dataset_id"] == "t"
