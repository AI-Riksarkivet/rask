"""General compression is opt-in PER TABLE and off by default ([[LH-034]]).

**THE DEFAULT IS A MEASURED DECISION, not an unset knob.** Lance applies FSST to variable-width data,
bitpacking to ints and RLE to low-cardinality columns on its own; `lance-encoding:compression` adds a
CLASSICAL compressor (lz4/zstd) on top of that, and it runs after the others. Measured on this estate's
own tier shape (`{id, payload, stage}`, 8 MiB of payload held constant while value size varies):

    value B      rows       none        lz4       zstd    lz4 vs none   zstd vs none
        128    65,536  3,055,712  4,699,767  3,112,063        +53.8%          +1.8%
        256    32,768  3,001,060  5,346,747  5,346,627        +78.2%         +78.2%
        512    16,384  2,995,490  3,900,282  3,900,226        +30.2%         +30.2%
      1,024     8,192  3,090,642  3,019,177  3,019,185         -2.3%          -2.3%
      2,048     4,096  3,051,730  2,419,690  2,419,698        -20.7%         -20.7%
      4,096     2,048  2,943,121  2,124,584  2,124,592        -27.8%         -27.8%

So it is not "compression on = smaller": below ~1 KiB per value it COSTS, peaking at +78%. The governed
tiers measured live on 2026-09-19 hold a payload median of 8 B (max 14 B) across 121 rows — squarely in
the region where a blanket scheme makes the estate bigger, which the real corpus confirmed at +15%.

The tier payload is OPAQUE by construction (`medallion/schemas/tier.py` declares `{id, payload, stage,
lineage, source_rowid}` and lets the transform decide the payload's shape), so there is no value size the
catalog can assume. A workload that knows its own opts in; the catalog imposes nothing.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from catalog.services.dataplane import _write_blob


_ENCODING = "lance-encoding:compression"


@pytest.fixture
def table() -> pa.Table:
    """The governed tier shape: a variable-width payload beside an int key and a low-cardinality stage."""
    return pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "payload": pa.array(["a", "b", "c"], pa.string()),
            "stage": pa.array(["bronze", "bronze", "silver"], pa.string()),
            "score": pa.array([0.5, 0.25, 0.125], pa.float64()),
        }
    )


def _write(table: pa.Table, tmp_path: Path, properties: dict[str, str] | None = None) -> lance.LanceDataset:
    return _write_blob(table, str(tmp_path / "t"), {}, mode="create", allow_external=False, external_blob_bases=[], properties=properties)


def _field_meta(dataset: lance.LanceDataset, name: str) -> dict[bytes, bytes]:
    return dataset.schema.field(name).metadata or {}


def test_a_plain_create_sets_no_compression_at_all(table: pa.Table, tmp_path: Path) -> None:
    """THE DEFAULT, pinned. A future edit that switches a scheme on for every table has to delete this
    test, and the docstring above says what it would cost."""
    dataset = _write(table, tmp_path)

    assert all(_ENCODING.encode() not in _field_meta(dataset, f) for f in ("id", "payload", "stage", "score"))


def test_the_property_reaches_the_variable_width_fields(table: pa.Table, tmp_path: Path) -> None:
    dataset = _write(table, tmp_path, {_ENCODING: "zstd"})

    assert _field_meta(dataset, "payload")[_ENCODING.encode()] == b"zstd"
    assert _field_meta(dataset, "stage")[_ENCODING.encode()] == b"zstd"


def test_it_does_NOT_reach_the_fixed_width_fields(table: pa.Table, tmp_path: Path) -> None:
    """`lance-encoding:bss` engages byte-stream-split on floats only where general compression is also
    applied, so stamping every field would change FLOAT encoding as a side effect of asking for string
    compression — a different physical layout than the caller asked for."""
    dataset = _write(table, tmp_path, {_ENCODING: "zstd"})

    assert _ENCODING.encode() not in _field_meta(dataset, "id")
    assert _ENCODING.encode() not in _field_meta(dataset, "score")


def test_an_ordinary_property_is_not_mistaken_for_an_encoding_knob(table: pa.Table, tmp_path: Path) -> None:
    """Properties are user-facing table metadata; only the `lance-encoding:` namespace is Lance's."""
    dataset = _write(table, tmp_path, {"owner": "platform", "lance-encoding:compression-level": "3"})

    meta = _field_meta(dataset, "payload")
    assert b"owner" not in meta
    assert meta[b"lance-encoding:compression-level"] == b"3"


def test_the_rows_survive_the_scheme(table: pa.Table, tmp_path: Path) -> None:
    """The point of the knob is bytes on disk, never a different answer — a compression setting that
    changed what the table returns would be a correctness bug wearing a performance flag."""
    dataset = _write(table, tmp_path, {_ENCODING: "zstd"})

    assert dataset.to_table().to_pydict() == table.to_pydict()
