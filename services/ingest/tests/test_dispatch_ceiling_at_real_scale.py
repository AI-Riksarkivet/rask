"""The dispatch payload stays inside grpc's ceiling at the scale this estate actually holds (B9).

`enumerate_chunks` returns ONE activity result carrying every chunk in the run, and an activity
result crosses the sidecar as one gRPC message against a 4 MiB default. `staging.write_unit_manifest`
already moved the unit list to object storage so descriptors are POINTERS — history is O(chunks), not
O(units) — and `CHUNK_DISPATCH_BUDGET_BYTES` (3 MiB) refuses before building a payload the transport
would reject.

B9 asked for the measurement that ceiling was never checked against. Here it is, against the estate's
stated scale rather than an assumed one: **10 million images and 50,000 hours of video** (owner,
2026-08-24). Measured, not estimated — the descriptor is serialised and counted.

    inline form (~83 B/unit)     10M units -> 791 MiB   refused
    pointer form (measured)      10M units -> 2.72 MiB  fits

THE HEADROOM IS THIN, AND THAT IS THE POINT OF THIS TEST. At CHUNK_SIZE=1000 the ceiling is ~11M
units, so 10M images sits at ~91% of the budget. Anything that grows a descriptor — one more field,
a longer dataset URI, a project id with more characters — moves a corpus this estate already holds
from "fits" to "REFUSED", and the failure surfaces as a RESOURCE_EXHAUSTED from inside the SDK on a
workflow that then retries four times and wedges, with nothing naming a knob.

The lever, when this fails, is CHUNK_SIZE — not the budget, which is derived from grpc's own limit.
Ten times the chunk size is ten times the ceiling and costs one more manifest read per chunk.
"""

from __future__ import annotations

import json

from ingest.workflow import CHUNK_DISPATCH_BUDGET_BYTES, CHUNK_SIZE, ChunkSpec


#: The estate's stated scale (owner, 2026-08-24): 10M images at 2-3 MB, 50k hours of video at ~500 MB.
IMAGES = 10_000_000
VIDEOS = 50_000


def _descriptor_bytes() -> int:
    """One pointer-form descriptor, serialised the way the activity result is.

    Built with realistically LONG values — a uuid run id, a `dir`-backend dataset URI — because a
    ceiling measured on short ids is a ceiling that does not hold in production.
    """
    spec = ChunkSpec(
        run_id="b9b753c6-7809-5a6c-8505-8a29c2be02fd",
        chunk_id="b9b753c6-7809-5a6c-8505-8a29c2be02fd-c9999",
        offset=9_999_000,
        count=CHUNK_SIZE,
        dataset_uri="s3://acme-bucket/3e5dacc9_acme-bronze$images",
        kind="s3-prefix",
        project="acme",
    )
    return len(json.dumps(spec.model_dump()))


def test_a_ten_million_unit_corpus_fits_the_dispatch_budget() -> None:
    """The scale this estate holds must not refuse at the enumeration seam."""
    chunks = -(-IMAGES // CHUNK_SIZE)
    payload = chunks * _descriptor_bytes()

    assert payload <= CHUNK_DISPATCH_BUDGET_BYTES, (
        f"{IMAGES:,} units serialises to {payload / 1024 / 1024:.2f} MiB against a "
        f"{CHUNK_DISPATCH_BUDGET_BYTES / 1024 / 1024:.0f} MiB budget. Raise CHUNK_SIZE (10x the size "
        "is 10x the ceiling, at one more manifest read per chunk) — do NOT raise the budget, which is "
        "derived from grpc's own 4 MiB default."
    )


def test_the_video_corpus_is_nowhere_near_the_budget() -> None:
    """50k units is two orders of magnitude under — recorded so the two scales are not conflated."""
    payload = -(-VIDEOS // CHUNK_SIZE) * _descriptor_bytes()

    assert payload < CHUNK_DISPATCH_BUDGET_BYTES // 10


def test_descriptors_are_pointers_and_carry_no_inline_units() -> None:
    """The property the ceiling depends on: `keys`/`tokens` are the LEGACY inline form.

    A new descriptor that populated them would put the whole unit set back into history and undo
    `write_unit_manifest` — 791 MiB at this estate's scale — while every size assertion above still
    passed, because they measure an EMPTY-keys descriptor.
    """
    spec = ChunkSpec(run_id="r", chunk_id="r-c0", offset=0, count=CHUNK_SIZE, dataset_uri="s3://b/t")

    assert spec.keys == []
    assert spec.tokens == []


def test_the_headroom_is_recorded_so_erosion_is_visible() -> None:
    """How many units fit today. Not a limit — a tripwire.

    This asserts the ceiling is at least the estate's stated scale plus 10%, so a descriptor that
    grows enough to endanger a corpus already held fails HERE, in a test naming the lever, rather
    than as a RESOURCE_EXHAUSTED inside the SDK on a wedged production run.
    """
    ceiling_units = (CHUNK_DISPATCH_BUDGET_BYTES // _descriptor_bytes()) * CHUNK_SIZE

    assert ceiling_units >= int(IMAGES * 1.1), (
        f"the dispatch ceiling is {ceiling_units:,} units, under {int(IMAGES * 1.1):,} (the estate's {IMAGES:,} plus 10% headroom). Raise CHUNK_SIZE."
    )
