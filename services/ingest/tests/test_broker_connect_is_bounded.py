"""A connect to an unreachable broker must give up, not wedge the run forever.

docs/DECISIONS.md "The Python estate audit" `ingest-flow-02` (E3, HIGH, effort S) — "Three of four NATS connect sites have no
timeout, against the file's own measured evidence that a connect to a dead broker never returns".

WHY IT WEDGES RATHER THAN FAILS. `publish_chunk_units`, `drain_chunk_units` and `reconcile_from_queue`
are the bodies of the `publish_units`, `drain_chunk` and `reconcile_chunk` activities. A Dapr activity
carries no execution timeout, so an unbounded connect hangs it indefinitely: nothing raises, so
`ACTIVITY_RETRY` never fires, the child workflow never returns, the parent's `when_all` never
completes, and the run sits RUNNING with no error recorded anywhere. That is strictly worse than a
failure — a failure is visible and retried.

THE PLANE HAD ALREADY MEASURED THE HAZARD AND FIXED ONE CALL. `runtime.RELEASE_TIMEOUT_SECONDS` and
`queue.inspect_queue` both record the same finding — "a connect to a dead address with
`connect_timeout`, `allow_reconnect=False` and `max_reconnect_attempts=0` ALL set had still not
returned after 60 seconds" — and both wrap their own call. The three activity bodies, the only sites
where a hang costs a run, did not.

SO THE BOUND GOES IN THE SEAM, not at the call sites: `WorkQueue.connect` wraps its own
`nats.connect`, every caller inherits it, and a fourth call site cannot forget it.

THE BLACK HOLE IS A REAL ONE. The fixture accepts the TCP connection and then sends nothing — no
`INFO` line — which is exactly the shape the measurement describes and the shape a client-side
`connect_timeout` was observed not to bound. An unroutable IP would not do: it can fail fast with
EHOSTUNREACH, which passes the test without the fix.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio


# @pytest_asyncio.fixture / @pytest.mark.asyncio throughout: rask does not set asyncio_mode="auto",
# so a plain @pytest.fixture on an async function yields the COROUTINE rather than the value.
@pytest_asyncio.fixture
async def black_hole() -> AsyncIterator[str]:
    """A TCP endpoint that accepts and then never speaks the NATS protocol."""
    server = await asyncio.start_server(lambda _r, _w: asyncio.sleep(3600), host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"nats://127.0.0.1:{port}"


@pytest.fixture
def short_bound(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the seam's bound so the suite proves the behaviour in a fraction of a second."""
    from ingest import queue as queue_mod

    monkeypatch.setattr(queue_mod, "CONNECT_TIMEOUT_SECONDS", 0.25)
    return 0.25


#: How long past the bound a call may take before the test calls it unbounded. Generous, because the
#: assertion is "it returns at all", not "it returns punctually".
_SLACK = 10.0


async def _must_raise_within(coro, bound: float, what: str) -> BaseException:
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=bound + _SLACK)
    except TimeoutError as exc:
        if time.monotonic() - started >= bound + _SLACK - 0.5:
            pytest.fail(f"{what} did not return within {bound + _SLACK}s against a black-hole broker — the activity would hang the run forever")
        return exc
    except Exception as exc:
        return exc
    pytest.fail(f"{what} returned successfully against a black-hole broker")


@pytest.mark.asyncio
async def test_the_seam_itself_gives_up(black_hole: str, short_bound: float) -> None:
    """`WorkQueue.connect` is where the bound belongs — every caller inherits it."""
    from ingest.queue import WorkQueue

    started = time.monotonic()
    await _must_raise_within(WorkQueue.connect(black_hole), short_bound, "WorkQueue.connect")
    assert time.monotonic() - started < short_bound + _SLACK


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["publish_chunk_units", "drain_chunk_units", "reconcile_from_queue"])
async def test_every_activity_body_raises_instead_of_hanging(activity: str, black_hole: str, short_bound: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """The three sites the finding names. They pass no options, so they inherit the seam's bound —
    which is the point: the fix must not depend on each call site remembering."""
    from ingest import runtime
    from ingest.workflow import ChunkSpec

    monkeypatch.setenv("RASK_NATS_URL", black_hole)
    chunk = ChunkSpec(run_id="r1", chunk_id="c1", count=1, keys=["k"], dataset_uri="memory://x")
    exc = await _must_raise_within(getattr(runtime, activity)(chunk), short_bound, activity)
    assert exc is not None, f"{activity} neither raised nor returned"
