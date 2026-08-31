"""A tier whose dataset has no STABLE ROW IDS cannot carry honest provenance — refuse it at publish.

The column contract already refuses a tier missing `stage`, `lineage` or `source_rowid`. It cannot see
the deeper failure: `source_rowid` holds a Lance stable row id, and `enable_stable_row_ids` is
CREATE-TIME ONLY — set it later and it is a silent no-op. So a dataset created without it carries a
`source_rowid` column full of values that are not stable, and the estate MEASURED what that costs:
after compaction the ids moved from `0..5` to `4294967296..`, silently naming rows that no longer
exist.

WHY THE CHECK BELONGS HERE AND NOT AT `register_table`. Three doors can produce a governed tier — the
catalog's own create (which sets the flag), the ingest plane (which refuses a dataset lacking it, gate
A14), and `register_table`, which checks nothing. Adding a fourth check at that third door would guard
one more entrance and still leave the property unenforced for anything arriving another way. The
publish door is the one every writer passes, and it already holds the contract this belongs to.

SAME RULE, extended: opt-in by CLAIM. A table carrying none of the tier columns is not a governed tier
and is untouched — an external dataset registered for discovery stays registrable. One that claims to
be a tier must be able to back the claim.
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
        pa.field("stage", pa.string()),
        pa.field("lineage", pa.string()),
        pa.field("source_rowid", pa.uint64()),
    ]
)
PLAIN = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])


def _rows(schema: pa.Schema) -> pa.Table:
    if schema is TIER:
        return pa.table(
            {
                "id": pa.array([1, 2], pa.int64()),
                "stage": pa.array(["silver"] * 2),
                "lineage": pa.array(['{"run_id":"r"}'] * 2),
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
    """The headline: the columns are all present and the provenance is still unbackable."""
    uri = _write(tmp_path, TIER, stable=False)

    with pytest.raises(InvalidInputError, match="stable row id"):
        gate(uri, key_column="id", version=1)


def test_a_tier_WITH_stable_row_ids_publishes(tmp_path: Path) -> None:
    """The guard against 'refuse everything' — a conforming tier must still pass."""
    result = gate(_write(tmp_path, TIER, stable=True), key_column="id", version=1)

    assert result.reason is None, f"a conforming tier was refused: {result.reason}"


def test_a_PLAIN_table_is_untouched(tmp_path: Path) -> None:
    """Opt-in by claim: a dataset that is not a governed tier stays registrable and publishable.

    This is the half that makes the rule safe at a door every writer passes — an external dataset
    registered for discovery neither claims provenance nor needs stable row ids.
    """
    result = gate(_write(tmp_path, PLAIN, stable=False), key_column="id", version=1)

    assert result.reason is None, f"a plain table was caught by the tier contract: {result.reason}"
