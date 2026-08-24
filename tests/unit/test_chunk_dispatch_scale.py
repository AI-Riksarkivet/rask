"""#6.8 — the MEASURED size of an `enumerate_chunks` result, and the ceiling it implies.

The chunk STRUCTURE is established (chunks carry a pointer into the run's unit manifest
instead of the keys themselves) and §6.8 recorded that its magnitude "is not establishable from
source — no gRPC limit and no state-store row limit is set anywhere in this repo. Measure before
sizing the fix."

RE-MEASURED 2026-08-24 at `CHUNK_SIZE=10000`, after the estate's own scale turned out to exceed the
old ceiling:

    units           chunks   result bytes   % of the 3 MiB dispatch budget
    1,000                1            403    0.0%
    100,000             10          4,030    0.1%
    1,000,000          100         40,300    1.3%      <- the advertised scale
    10,000,000       1,000        403,000   12.8%      <- the estate's stated scale
    100,000,000     10,000      4,030,000  128.1%      <- OVER budget

    budget reached at 78,050,000 units (7,805 chunks), at 403 B per descriptor

WHY THE CHUNK SIZE MOVED. At `CHUNK_SIZE=1000` the ceiling was ~9.9M units and the owner stated the
estate holds "over 10 million images" (2026-08-24) — so a corpus already in the building serialised
to 3.02 MiB against the 3 MiB budget and would have been REFUSED at the enumeration seam. Note the
direction, which is the counter-intuitive part: a SMALLER chunk size means MORE descriptors in the
one activity result, so shrinking chunks LOWERS the ceiling. 10000 is the top of the plan's own
1-10k range and satisfies this suite's own 5x-headroom rule with ~7.8x.

CORRECTED 2026-08-15. The first numbers here (8.4% at 1M, ceiling 11.8M) came from a fixture that
omitted five of the ten fields `enumerate_chunks` actually sets. That correction is why the 2026-08-24
re-measure was trusted: the same fixture-completeness error was nearly repeated, with a hand-built
partial dict giving 285 B against the real model_dump's 317 B.

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

from ingest.sizing import resolve
from ingest.workflow import CHUNK_DISPATCH_BUDGET_BYTES, CHUNK_SIZE, GRPC_MAX_MESSAGE_BYTES, ChunkSpec


#: The scale this plane's docstrings advertise ("the million-unit harvest").
ADVERTISED_UNITS = 1_000_000


def _result_bytes(units: int) -> tuple[int, int]:
    """`(chunks, bytes)` for the activity's ACTUAL return — a list of serialized ChunkSpec dicts.

    EVERY field `enumerate_chunks` populates (workflow.py:875-887), not the four that are obvious. The
    first version of this helper set only run_id/chunk_id/offset/count/dataset_uri and omitted
    `sizing` (a nested ResolvedSizing), `kind`, `project`, `dataset` and `options` — so it
    under-measured the real payload by ~1.7x and reported a ceiling nearly double the true one. A
    fixture that is cheaper than the thing it measures does not measure it.

    Serialized rather than sized on the model, because what crosses the gRPC boundary and lands in
    workflow history is the JSON.
    """
    n = (units + CHUNK_SIZE - 1) // CHUNK_SIZE
    sizing = resolve(None)
    chunks = [
        ChunkSpec(
            run_id="r-0123456789abcdef",
            chunk_id=f"r-0123456789abcdef-c{i}",
            offset=i * CHUNK_SIZE,
            count=min(CHUNK_SIZE, units - i * CHUNK_SIZE),
            dataset_uri="s3://bind86-wh/bind86-bronze/pages.lance",
            sizing=sizing,
            kind="iiif",
            project="bind86",
            dataset="pages",
            options={"root": "s3://bind86-wh/raw/volumes/SE_RA_420001_01_A_0001", "recursive": True},
        ).model_dump()
        for i in range(n)
    ]
    return n, len(json.dumps(chunks, default=str).encode())


def test_the_ADVERTISED_million_unit_harvest_fits_with_room_to_spare() -> None:
    """The question §6.8 asked, answered with a number rather than a structure.

    8.4% of budget at a million units. If this ever approaches 100%, the plane cannot dispatch the
    scale its own docstrings promise — and it would fail at `_refuse_oversized_dispatch`, which is a
    refusal, not a crash, but is still a harvest that does not run.
    """
    chunks, size = _result_bytes(ADVERTISED_UNITS)

    assert chunks == 100
    assert size < CHUNK_DISPATCH_BUDGET_BYTES // 4, (
        f"a {ADVERTISED_UNITS:,}-unit run now serializes to {size:,} B — more than a quarter of the "
        f"{CHUNK_DISPATCH_BUDGET_BYTES:,} B dispatch budget. Something is carrying per-unit data on the "
        f"chunk descriptor again; the pointer design (§2.13) is what keeps this O(chunks)."
    )


def test_the_ceiling_is_MILLIONS_of_units_not_tens_of_thousands() -> None:
    """The property the pointer redesign bought — and the honest edge of it.

    Pre-§2.13 the budget was exhausted at ~45k units because each chunk carried its 1000 keys. It is
    now ~7M. That is the difference between "needs a different design immediately" and "needs one at a
    scale nobody has asked for", which is the property worth holding.

    Asserted at 5M rather than 10M ON PURPOSE: 10M genuinely does NOT fit (143% of budget), and the
    earlier version of this test claimed it did because its fixture was missing five fields. An
    assertion that passes only because the fixture is wrong is worse than no assertion.
    """
    _, at_five_million = _result_bytes(5_000_000)

    assert at_five_million < CHUNK_DISPATCH_BUDGET_BYTES, (
        f"5M units serializes to {at_five_million:,} B, over the {CHUNK_DISPATCH_BUDGET_BYTES:,} B budget — "
        f"the dispatch ceiling has fallen back toward the range the pointer redesign moved it out of"
    )


def test_a_hundred_million_units_does_NOT_fit_and_that_is_recorded_not_hidden() -> None:
    """The limit stated as a fact, so nobody plans a harvest on a wrong number.

    THE NUMBER MOVED, THE INVARIANT DID NOT (2026-08-24). This asserted 10M refused — true at
    CHUNK_SIZE=1000 and false once the estate's stated scale forced that knob to 10000. This test
    told its own successor what to do ("re-measure and update the docstring table rather than
    deleting the assertion"), and that is what happened: the table above is re-measured and the
    assertion now sits past the new ceiling instead of at the old one.

    `_refuse_oversized_dispatch` catches it at runtime — a refusal, not a crash — but a plane should
    say where its advertised scale stops working.
    """
    _, at_hundred_million = _result_bytes(100_000_000)

    assert at_hundred_million > CHUNK_DISPATCH_BUDGET_BYTES, (
        "100M units now FITS the dispatch budget. Good news, but this test records a measured limit — "
        "re-measure and update the docstring table rather than deleting the assertion."
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
