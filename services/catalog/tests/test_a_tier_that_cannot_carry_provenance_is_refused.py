"""A table whose dataset has no STABLE ROW IDS cannot carry honest `source_rowid` provenance — refuse it at publish.

`source_rowid` holds a Lance stable row id, and `enable_stable_row_ids` is CREATE-TIME ONLY — set it later and it
is a silent no-op. So a dataset created without it carries a `source_rowid` column full of values that are not
stable, and the estate MEASURED what that costs: after compaction the ids moved from `0..5` to `4294967296..`,
silently naming rows that no longer exist.

The publish door is the one every writer passes, so the check lives there. Opt-in by the column: a table without
`source_rowid` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace.errors import InvalidInputError

from catalog.services.publication import gate


lance = pytest.importorskip("lance")

TIER = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_rowid", pa.uint64()),
    ]
)


def _rows(schema: pa.Schema) -> pa.Table:
    if schema is TIER:
        return pa.table(
            {
                "id": pa.array([1, 2], pa.int64()),
                "source_rowid": pa.array([7, 8], pa.uint64()),
            },
            schema=schema,
        )
    return pa.table({"id": pa.array([1, 2], pa.int64()), "payload": pa.array(["a", "b"])}, schema=schema)


def _write(tmp_path: Path, schema: pa.Schema, *, stable: bool) -> str:
    uri = str(tmp_path / f"{'stable' if stable else 'unstable'}.lance")
    lance.write_dataset(_rows(schema), uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=stable)
    return uri


def test_a_tier_without_stable_row_ids_is_REFUSED(tmp_path: Path) -> None:
    """The headline: the column is well typed and the provenance is still unbackable."""
    uri = _write(tmp_path, TIER, stable=False)

    with pytest.raises(InvalidInputError, match="stable row id"):
        gate(uri, key_column="id", version=1)
