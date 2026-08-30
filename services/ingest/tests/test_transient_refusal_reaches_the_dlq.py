"""A persistently-transient fetch failure must back off, and then park — never vanish.

open_python-audit `ingest-flow-05` (E3, med). `_refuse` split PERMANENT (park to the DLQ + ack) from
transient (`nak`), which was the right shape but left two holes the module's own docstring promises
are covered:

1. THE NAK HAD NO DELAY. `msg.nak()` redelivers immediately, so a unit failing against a
   rate-limited or briefly-down endpoint re-hit it at once — the queue's `max_ack_pending`
   backpressure exists precisely to protect that endpoint, and an undelayed nak spends it.

2. AN EXHAUSTED TRANSIENT WAS DROPPED SILENTLY. JetStream stops redelivering after `MAX_DELIVER`
   attempts and does NOT auto-park to a subject — it just drops. So a failure that stays transient
   for the whole window (the endpoint is down, not the page) exhausted its deliveries and the unit
   was gone: not parked to `dlq.ingest.tasks`, not in `outcome.errors`, invisible to the publish
   precondition. queue.py's own docstring says "redeliver forever — hence max_deliver and the
   dlq.ingest.tasks parking subject", which is only true if the LAST attempt parks. It did not.

The fix keeps the permanent path unchanged and gives the transient path a delay plus a terminal:
on the final delivery a transient failure parks to the DLQ, records the error, and acks — the same
visible outcome a permanent failure gets, rather than a silent loss.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from ingest.queue import MAX_DELIVER, UnitTask
from ingest.worker import ChunkOutcome, Worker


class _Msg:
    def __init__(self, num_delivered: int) -> None:
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.naks: list[float | None] = []
        self.acked = False

    async def nak(self, delay: float | None = None) -> None:
        self.naks.append(delay)

    async def ack(self) -> None:
        self.acked = True


class _SpyQueue:
    def __init__(self) -> None:
        self.parked: list[tuple[str, str]] = []

    async def park_poison(self, task: UnitTask, reason: str) -> None:
        self.parked.append((task.key, reason))


def _worker() -> tuple[Worker, _SpyQueue]:
    # `_refuse` reads only `self._q.park_poison`, so the spy satisfies the one method under test;
    # cast keeps that structural substitution out of ty's view without a suppression it ignores.
    from ingest.queue import WorkQueue

    queue = _SpyQueue()
    worker = Worker(cast("WorkQueue", queue), fetcher=cast("Any", SimpleNamespace(fetch=None)))
    return worker, queue


def _transient() -> Exception:
    """A 503 — unrecognised-as-permanent, so `_is_permanent` presumes it transient."""
    request = httpx.Request("GET", "http://src.invalid/x")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(503, request=request))


@pytest.mark.asyncio
async def test_a_transient_nak_carries_a_backoff_delay() -> None:
    worker, _ = _worker()
    msg = _Msg(num_delivered=1)
    outcome = ChunkOutcome(chunk_id="c0")

    await worker._refuse(msg, UnitTask(run_id="r", key="k", chunk_id="c0", dataset_uri="memory://x"), _transient(), outcome)

    assert msg.naks == [pytest.approx(msg.naks[0])] and msg.naks[0], "the nak carried no delay — an undelayed redelivery hammers the rate-limited endpoint"
    assert not msg.acked, "an early transient failure was acked instead of redelivered"


@pytest.mark.asyncio
async def test_the_last_transient_delivery_parks_instead_of_vanishing() -> None:
    """At MAX_DELIVER the next nak would be dropped by JetStream — so the terminal attempt must park
    the unit to the DLQ and record it, the way the docstring promises."""
    worker, queue = _worker()
    msg = _Msg(num_delivered=MAX_DELIVER)
    outcome = ChunkOutcome(chunk_id="c0")

    await worker._refuse(msg, UnitTask(run_id="r", key="k", chunk_id="c0", dataset_uri="memory://x"), _transient(), outcome)

    assert queue.parked and queue.parked[0][0] == "k", "an exhausted transient was not parked — it is silently dropped by JetStream"
    assert "k" in outcome.errors, "the exhausted unit is missing from outcome.errors — invisible to the publish precondition"
    assert msg.acked, "the parked unit was not acked, so JetStream will redeliver a unit already in the DLQ"
    assert msg.naks == [], "the terminal attempt naked (and will be dropped) instead of parking"
