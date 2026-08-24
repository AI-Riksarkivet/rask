"""B9, measured: the dispatch payload is O(chunks), not O(units) — and the plan's figure was 275x off.

`open_batch_process.md` B9 asked for an oversized activity result to become a HANDLE, and required the
threshold to be MEASURED rather than guessed — its own precondition, because the 120 MB figure in the
text was admitted arithmetic rather than an observation.

Measured here at the real descriptor shape:

       units    chunks          bytes  bytes/unit
   1,000,000     1,000        434,777       0.435
   5,000,000     5,000      2,182,777       0.437
  10,000,000    10,000      4,367,777       0.437   over budget

The budget is reached at ~7,203,001 units. So at the million-unit harvests this plane advertises the
payload is 0.43 MB against a 3 MiB budget — not 120 MB. The arithmetic assumed the result carries the
KEYS; it does not, and has not since `write_unit_manifest` moved the (key, token) list to object
storage and left the descriptors carrying `offset` and `count`. THAT is the handle B9 asked for; it
already exists, and this measurement is what shows the remaining payload does not need a second one.

So no `RASK_WF_INLINE_MAX_BYTES` is introduced. Adding a ceiling above a payload with 7x headroom
would be config nothing reads — the dead-config defect this estate has been bitten by twice — and the
existing budget is already measured at the point of use (`len(json.dumps(chunks).encode())`, the same
serialization the SDK performs on the way out) rather than estimated from a per-key constant.

WHAT THIS FILE ACTUALLY GUARDS is the property that makes all of the above true: descriptors are
POINTERS. The moment one starts carrying per-unit data again, the payload returns to O(units) and the
120 MB arithmetic becomes correct after all — silently, because nothing else measures it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ingest.workflow import CHUNK_DISPATCH_BUDGET_BYTES, CHUNK_SIZE, GRPC_MAX_MESSAGE_BYTES, ChunkSpec


def _descriptors(units: int) -> list[dict[str, Any]]:
    """The real shape `enumerate_chunks` builds, with realistic ids and a realistic dataset URI."""
    sizing = {
        "fragment_rows": 1024,
        "max_rows_per_file": 100_000,
        "fragment_bytes": 67_108_864,
        "fetch_batch": 32,
        "fetch_concurrency": 8,
    }
    return [
        ChunkSpec(
            run_id="01JD8Q2K7F3Z9YB4X6M0PNRTVW",
            chunk_id=f"01JD8Q2K7F3Z9YB4X6M0PNRTVW-c{index // CHUNK_SIZE}",
            offset=index,
            count=min(CHUNK_SIZE, units - index),
            dataset_uri="s3://acme-bucket/acme-bronze$pages.lance",
            sizing=sizing,
            kind="s3-prefix",
            project="acme",
            dataset="pages",
            options={"bucket": "acme-raw", "prefix": "volumes/2026/"},
        ).model_dump()
        for index in range(0, units, CHUNK_SIZE)
    ]


def _serialized(units: int) -> int:
    return len(json.dumps(_descriptors(units)).encode())


class TestThePayloadIsPerChunkNotPerUnit:
    def test_a_million_units_fits_with_room_to_spare(self) -> None:
        """The advertised scale. 0.43 MB against a 3 MiB budget."""
        size = _serialized(1_000_000)
        assert size < CHUNK_DISPATCH_BUDGET_BYTES // 4, f"1M units serialized to {size} bytes"

    def test_ten_x_the_units_costs_ten_x_the_bytes_not_more(self) -> None:
        """Linear in CHUNKS. Super-linear growth would mean a descriptor started carrying per-unit
        data, which is the regression this file exists for."""
        small, large = _serialized(100_000), _serialized(1_000_000)
        assert large < small * 11, f"{small} -> {large} is worse than linear"

    def test_the_descriptor_carries_no_per_unit_data(self) -> None:
        """The structural half of the same claim: one chunk of 1000 units must not be 1000x one unit."""
        one_chunk = _descriptors(CHUNK_SIZE)
        assert len(one_chunk) == 1
        body = json.dumps(one_chunk[0])
        assert len(body) < 1024, f"a single chunk descriptor is {len(body)} bytes — it is carrying units"
        assert "key" not in one_chunk[0], "keys belong in the unit manifest, not the dispatch payload"

    @pytest.mark.parametrize("units", [1_000, 100_000, 1_000_000])
    def test_bytes_per_unit_stays_under_one(self, units: int) -> None:
        assert _serialized(units) / units < 1.0


class TestTheBudgetStillBites:
    def test_a_run_far_past_the_advertised_scale_is_over_budget(self) -> None:
        """The ceiling is real, not decorative — it still refuses, just further out.

        NUMBER CHANGED, INVARIANT UNCHANGED (2026-08-24). This asserted 10M refused, which was true
        at CHUNK_SIZE=1000 and became FALSE when the estate's own scale turned out to be "over 10
        million images" (owner) — a corpus already in the building serialised to 3.02 MiB against
        the 3 MiB budget and would have been refused at the enumeration seam. CHUNK_SIZE went to
        10000 and the ceiling to ~99M.

        What this test protects is that a ceiling EXISTS, so it now asserts at a scale past the new
        one rather than at the old boundary. Deleting it would remove the only guard against the
        budget being quietly raised to whatever the payload happens to be.
        """
        assert _serialized(200_000_000) > CHUNK_DISPATCH_BUDGET_BYTES

    def test_the_budget_sits_below_the_grpc_ceiling(self) -> None:
        """Deliberately under, not at: what grpc weighs is this payload PLUS the durabletask envelope,
        and a budget set at the limit would refuse nothing until the envelope pushed it over."""
        assert CHUNK_DISPATCH_BUDGET_BYTES < GRPC_MAX_MESSAGE_BYTES

    def test_the_headroom_is_at_least_five_times_the_advertised_scale(self) -> None:
        """Why no second ceiling is introduced: a knob above this much headroom reads nothing."""
        assert _serialized(5_000_000) < CHUNK_DISPATCH_BUDGET_BYTES
