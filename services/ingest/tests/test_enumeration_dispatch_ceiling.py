"""The enumeration dispatch ceiling — the guard that could not sit where it was.

`max_units` was checked in the WORKFLOW body, which only runs once `enumerate_chunks`' result has
been DELIVERED. That result carries every key and token in the run as one gRPC message, and the Dapr
workflow worker builds its channel with `DEFAULT_GRPC_KEEPALIVE_OPTIONS` and nothing else — so grpc's
4 MiB default stands, and a large-enough run fails on the way back with `RESOURCE_EXHAUSTED` from
inside the SDK, four times, before wedging. The guard sat behind the failure it guarded against.

These pin the refusal at the only place it can work: inside the activity, before the payload exists.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ingest.workflow import (
    CHUNK_DISPATCH_BUDGET_BYTES,
    GRPC_MAX_MESSAGE_BYTES,
    REFUSAL_KEY,
    _refuse_oversized_dispatch,
)


def _chunk(keys: int) -> dict[str, Any]:
    return {"keys": [f"s3://bucket/some/reasonably-long/object-key-{i:09d}.tif" for i in range(keys)], "tokens": ["etag" * 8] * keys}


def test_a_payload_over_the_budget_is_refused() -> None:
    """The wedge, made a refusal. Sized from real key/token shapes rather than a synthetic blob, so
    the threshold is exercised the way a source reaches it."""
    chunks = [_chunk(1000) for _ in range(80)]
    assert len(json.dumps(chunks).encode()) > CHUNK_DISPATCH_BUDGET_BYTES, "fixture is not actually oversized"

    refusal = _refuse_oversized_dispatch(chunks, units=80_000, max_units=0)

    assert refusal is not None
    assert "RASK_INGEST_MAX_UNITS" in refusal[REFUSAL_KEY], "the refusal must name the knob that bounds it"
    assert str(GRPC_MAX_MESSAGE_BYTES) in refusal[REFUSAL_KEY], "the refusal must name the ceiling it hit"


def test_the_refusal_itself_always_fits() -> None:
    """The refusal replaces a payload that could not be delivered, so it must be small enough to be
    delivered — a refusal that also wedged would be the same bug with a better message."""
    refusal = _refuse_oversized_dispatch([_chunk(1000) for _ in range(80)], units=80_000, max_units=0)

    assert refusal is not None
    assert len(json.dumps(refusal).encode()) < 4096


def test_the_budget_leaves_headroom_under_the_grpc_ceiling() -> None:
    """What grpc weighs is this payload PLUS the durabletask envelope around it. A budget set AT the
    limit would refuse nothing until the envelope pushed it over, which is the failure this exists to
    make impossible."""
    assert CHUNK_DISPATCH_BUDGET_BYTES < GRPC_MAX_MESSAGE_BYTES
    assert GRPC_MAX_MESSAGE_BYTES - CHUNK_DISPATCH_BUDGET_BYTES >= 512 * 1024


def test_a_payload_under_the_budget_proceeds() -> None:
    """The guard must not refuse the ordinary run — the plane advertises long harvests."""
    assert _refuse_oversized_dispatch([_chunk(1000) for _ in range(5)], units=5_000, max_units=0) is None


def test_the_declared_ceiling_is_reported_before_the_transport_one() -> None:
    """A deployment that set a sane ceiling must read the message it configured, not one about gRPC.
    Both apply to this payload; the operator's intent is the one that explains it."""
    chunks = [_chunk(1000) for _ in range(80)]

    refusal = _refuse_oversized_dispatch(chunks, units=80_000, max_units=1_000)

    assert refusal is not None
    assert "over the 1000 ceiling" in refusal[REFUSAL_KEY]
    assert "grpc" not in refusal[REFUSAL_KEY].lower()


@pytest.mark.parametrize("max_units", [0, 100_000])
def test_a_small_run_is_never_refused_by_either_ceiling(max_units: int) -> None:
    assert _refuse_oversized_dispatch([_chunk(10)], units=10, max_units=max_units) is None
