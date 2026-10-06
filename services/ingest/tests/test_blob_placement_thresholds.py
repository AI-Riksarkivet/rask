"""Where a payload physically lands, and why the numbers are PINNED rather than inherited.

Lance blob v2 has four placements. Three of them are size-driven and the boundaries were, until this
file existed, taken on trust from a comment — a comment that was wrong in both numbers and therefore
wrong in its conclusion. It said the defaults were 16 KiB / 2 MiB and that "a scanned archival page
lands in `dedicated`". Measured on pylance 10.0.0, a 20 KB payload lands INLINE and a 3 MB scanned
page lands PACKED, so the estate believed its page images each had a dedicated sidecar while they
were in fact sharing packed ones.

**Why pinning matters more here than in most places.** `lance_docs/guide.md` states these thresholds
are stored in the dataset SCHEMA, and that "appends that explicitly provide different threshold
metadata for the same column are rejected". An inherited default that shifts under a library upgrade
therefore does not merely retune new writes — it splits an existing table's appends from the schema
they were created against, with no code change anywhere to point at.

These tests are deliberately written against LANCE, not against our constant: asserting that our
constant equals our constant would pass forever while the format moved underneath it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field

from ingest.runtime import BLOB_DEDICATED_SIZE_THRESHOLD, BLOB_INLINE_SIZE_THRESHOLD, BRONZE_SCHEMA


#: The descriptor's `kind`, measured rather than assumed — the numbering is not documented and is not
#: the order the tiers are usually listed in. `2` is inferred from the two neighbours being observed
#: and a >= 4 MiB payload being neither; `3` is confirmed directly by the external-placement suite.
INLINE, PACKED, DEDICATED, EXTERNAL = 0, 1, 2, 3


def _placement(payload_size: int, tmp_path: Path, *, inline: int | None = None, dedicated: int | None = None) -> int:
    """Write ONE row of `payload_size` bytes and report the placement Lance chose."""
    field = blob_field("payload", nullable=False, inline_size_threshold=inline, dedicated_size_threshold=dedicated)
    schema = pa.schema([pa.field("id", pa.int64()), field])
    table = pa.table({"id": pa.array([0], pa.int64()), "payload": blob_array([b"X" * payload_size])}, schema=schema)
    uri = str(Path(tempfile.mkdtemp(dir=tmp_path)) / "probe.lance")
    lance.write_dataset(table, uri, data_storage_version="2.2")
    return int(lance.dataset(uri).to_table(columns=["payload"]).column("payload")[0].as_py()["kind"])


class TestTheNamedThresholdsAreTheOnesLanceApplies:
    """Against the FORMAT, so a pylance retune fails here rather than in a cluster."""

    @pytest.mark.parametrize(
        ("size", "expected", "why"),
        [
            (BLOB_DEDICATED_SIZE_THRESHOLD - 262_144, PACKED, "just under the dedicated floor"),
            (BLOB_DEDICATED_SIZE_THRESHOLD + 262_144, DEDICATED, "over it — this payload gets its own .blob"),
        ],
    )
    def test_each_band_lands_where_the_constant_says(self, size: int, expected: int, why: str, tmp_path: Path) -> None:
        actual = _placement(size, tmp_path, inline=BLOB_INLINE_SIZE_THRESHOLD, dedicated=BLOB_DEDICATED_SIZE_THRESHOLD)
        assert actual == expected, f"{size:,} B ({why}) landed kind={actual}, expected kind={expected}"


class TestTheBronzeSchemaCarriesThem:
    """Stored in the schema, which is what makes them a create-time contract rather than a knob."""

    def test_the_recorded_values_are_the_named_constants(self) -> None:
        """Guards the wiring, not the numbers — a `blob_field` call that dropped one of the keyword
        arguments would leave the column silently inheriting whatever the library does that week."""
        metadata = BRONZE_SCHEMA.field("payload").metadata or {}
        assert int(metadata[b"lance-encoding:blob-inline-size-threshold"]) == BLOB_INLINE_SIZE_THRESHOLD
        assert int(metadata[b"lance-encoding:blob-dedicated-size-threshold"]) == BLOB_DEDICATED_SIZE_THRESHOLD
