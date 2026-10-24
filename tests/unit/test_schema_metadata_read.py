"""#74 tail read-back: the catalog reads table schema-metadata so the Table Properties UI can display it.

pylance 8.0.0's describe_table leaves DescribeTableResponse.metadata empty, so the editor seeded blank
(browser-driven find 2026-07-21). ``user_schema_metadata``, which describe's detailed metadata reads, fills it from
the dataset's Arrow schema metadata, decoding bytes and excluding internal ``lineage.*`` bookkeeping keys.
"""

from __future__ import annotations

import pyarrow as pa

from catalog.services import dataplane


def test_excludes_internal_lineage_keys() -> None:
    # The lineage.* keys are catalog/lineage bookkeeping stamped at create — not user properties.
    schema = pa.schema(
        [pa.field("id", pa.int64())],
        metadata={
            b"proof": b"live",
            b"lineage.dataset_id": b"db$t",
            b"lineage.namespace": b"db",
            b"lineage.create_run_id": b"abc",
        },
    )
    out = dataplane.user_schema_metadata(schema)
    assert out == {"proof": "live"}  # only the user key survives
