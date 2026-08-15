"""#6.8 — the MEASURED size of an `enumerate_chunks` result, and the ceiling it implies.

`open_dapr.md` §2.13 established the STRUCTURE (chunks carry a pointer into the run's unit manifest
instead of the keys themselves) and §6.8 recorded that its magnitude "is not establishable from
source — no gRPC limit and no state-store row limit is set anywhere in this repo. Measure before
sizing the fix."

Measured 2026-08-15, serializing the real `ChunkSpec` at `CHUNK_SIZE=1000`:

    units          chunks   result bytes   % of the 3 MiB dispatch budget
    1,000               1            257    0.0%
    10,000             10          2,597    0.1%
    100,000           100         26,177    0.8%
    1,000,000       1,000        263,777    8.4%      <- the advertised scale
    10,000,000     10,000      2,657,777   84.5%

    budget reached at 11,820,001 units (11,821 chunks)

And the shape it replaced, with keys carried inline at a realistic S3 key length (~70 B/unit):

    budget reached at 44,849 units

So the pointer redesign moved the ceiling by ~264x, and the docstring's "roughly 38k units" estimate
for the old shape was close to the measured 44.8k.

WHY THIS IS A TEST AND NOT A NOTE. The numbers above are only true while a chunk stays a POINTER. Any
change that puts per-unit data back on the descriptor — keys, per-unit errors, a manifest inlined "just
for debugging" — collapses the ceiling back toward tens of thousands, and nothing else in the estate
would notice until a large harvest failed at dispatch. `_refuse_oversized_dispatch` catches it at
runtime, which is the right last line of defence and the wrong place to FIND OUT.
"""

from __future__ import annotations

import json

from ingest.workflow import CHUNK_DISPATCH_BUDGET_BYTES, CHUNK_SIZE, GRPC_MAX_MESSAGE_BYTES, ChunkSpec


#: The scale this plane's docstrings advertise ("the million-unit harvest").
ADVERTISED_UNITS = 1_000_000


def _result_bytes(units: int) -> tuple[int, int]:
    """`(chunks, bytes)` for the activity's ACTUAL return — a list of serialized ChunkSpec dicts.

    Serialized rather than measured on the model, because what crosses the gRPC boundary and lands in
    workflow history is the JSON, not the object.
    """
    n = (units + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = [
        ChunkSpec(
            run_id="r-0123456789abcdef",
            chunk_id=f"r-0123456789abcdef-c{i}",
            offset=i * CHUNK_SIZE,
            count=min(CHUNK_SIZE, units - i * CHUNK_SIZE),
            dataset_uri="s3://bind86-wh/bind86-bronze/pages.lance",
        ).model_dump()
        for i in range(n)
    ]
    return n, len(json.dumps(chunks).encode())


def test_the_ADVERTISED_million_unit_harvest_fits_with_room_to_spare() -> None:
    """The question §6.8 asked, answered with a number rather than a structure.

    8.4% of budget at a million units. If this ever approaches 100%, the plane cannot dispatch the
    scale its own docstrings promise — and it would fail at `_refuse_oversized_dispatch`, which is a
    refusal, not a crash, but is still a harvest that does not run.
    """
    chunks, size = _result_bytes(ADVERTISED_UNITS)

    assert chunks == 1_000
    assert size < CHUNK_DISPATCH_BUDGET_BYTES // 4, (
        f"a {ADVERTISED_UNITS:,}-unit run now serializes to {size:,} B — more than a quarter of the "
        f"{CHUNK_DISPATCH_BUDGET_BYTES:,} B dispatch budget. Something is carrying per-unit data on the "
        f"chunk descriptor again; the pointer design (§2.13) is what keeps this O(chunks)."
    )


def test_the_ceiling_is_MILLIONS_of_units_not_tens_of_thousands() -> None:
    """The property the pointer redesign bought, asserted where a regression would show.

    Pre-§2.13 the same budget was exhausted at ~45k units because each chunk carried its 1000 keys.
    Ten million units is the scale at which this plane would need a different design; forty thousand
    is the scale at which it would need one immediately.
    """
    _, at_ten_million = _result_bytes(10_000_000)

    assert at_ten_million < CHUNK_DISPATCH_BUDGET_BYTES, (
        f"10M units serializes to {at_ten_million:,} B, over the {CHUNK_DISPATCH_BUDGET_BYTES:,} B budget — "
        f"the dispatch ceiling has fallen back into the range the pointer redesign moved it out of"
    )


def test_a_chunk_descriptor_carries_NO_PER_UNIT_DATA() -> None:
    """The structural reason the numbers above hold, checked directly rather than inferred from them.

    A size assertion alone would pass if someone added a small per-unit field and simultaneously
    lowered CHUNK_SIZE. This says the thing that actually matters: one chunk's serialized size does
    not depend on how many units it covers.
    """
    small = ChunkSpec(run_id="r", chunk_id="r-c0", offset=0, count=1, dataset_uri="s3://b/d.lance")
    large = ChunkSpec(run_id="r", chunk_id="r-c0", offset=0, count=1_000_000, dataset_uri="s3://b/d.lance")

    assert len(json.dumps(small.model_dump())) == len(json.dumps(large.model_dump())) - len("999999"), (
        "a chunk's size grew with its unit COUNT — the descriptor is carrying per-unit data, which is exactly what §2.13 removed"
    )


def test_the_budget_leaves_headroom_under_the_grpc_ceiling() -> None:
    """The budget is deliberately below grpc's limit, not equal to it.

    The activity result is not the only thing on the wire — Dapr wraps it with its own envelope, and a
    budget set AT the ceiling would fail on the framing rather than on the payload, which is a much
    harder failure to read.
    """
    assert CHUNK_DISPATCH_BUDGET_BYTES < GRPC_MAX_MESSAGE_BYTES
    assert GRPC_MAX_MESSAGE_BYTES - CHUNK_DISPATCH_BUDGET_BYTES >= 1024 * 1024, "less than a MiB of framing headroom"
