"""A DLQ that cannot take a poison unit must not take the whole run down with it.

§ Q3-6 (`ingest-flow-06`, med). `park_poison` awaited `js.publish` UNWRAPPED, and all three of the
worker's parking paths await it BEFORE `msg.ack()`. So any failure of that publish — the DLQ stream
absent, the broker briefly gone, a `limits`-retention stream at its ceiling refusing the write —
raised out of the drain task: the unit was never acked, the chunk that was supposed to "complete
WITH ERRORS rather than hang" hung, and ONE bad unit failed the entire run.

`ensure_dlq_stream` (added for the absent-stream case) narrows the window and does not close it. It
runs once at drain start; the publish happens later and can fail for reasons provisioning cannot
pre-empt.

THE ORDERING THAT DECIDES THE FIX, and it is why acking anyway is the honest answer rather than a
shortcut: the unit's failure is recorded in `outcome.errors[task.key]` BEFORE the park, and that
record is what the publish precondition reads. The DLQ copy is EVIDENCE, not the authoritative
record. Losing the evidence loudly — logged ERROR, and named in the run's own error text — is
strictly better than hanging a run that has already landed everything else it fetched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from ingest.queue import MAX_DELIVER, UnitTask, WorkQueue
from ingest.worker import ChunkOutcome, Worker


class _RefusingJetStream:
    """A JetStream whose publish always fails — the shape a missing or full DLQ stream presents."""

    def __init__(self) -> None:
        self.attempts = 0

    async def publish(self, *_args: Any, **_kwargs: Any) -> Any:
        self.attempts += 1
        raise RuntimeError("no responders available for request")


class _Msg:
    def __init__(self, num_delivered: int = 1) -> None:
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.acked = False
        self.naks: list[float | None] = []

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay: float | None = None) -> None:
        self.naks.append(delay)


class _UnparkableQueue:
    """The queue AS THE WORKER MUST SEE IT once the park itself can fail: it answers, it never raises."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def park_poison(self, task: UnitTask, reason: str) -> bool:
        self.attempts.append(task.key)
        return False


def _task() -> UnitTask:
    return UnitTask(run_id="r", key="k", chunk_id="c0", dataset_uri="memory://x")


def _permanent() -> Exception:
    request = httpx.Request("GET", "http://src.invalid/x")
    return httpx.HTTPStatusError("gone", request=request, response=httpx.Response(410, request=request))


def _transient() -> Exception:
    request = httpx.Request("GET", "http://src.invalid/x")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(503, request=request))


@pytest.mark.asyncio
async def test_park_poison_reports_a_failed_park_instead_of_raising_it() -> None:
    """The seam itself. A publish that fails is an ANSWER — False — not an exception the drain wears."""
    js = _RefusingJetStream()
    queue = WorkQueue(cast("Any", SimpleNamespace(close=None)), cast("Any", js))

    parked = await queue.park_poison(_task(), "corrupt payload")

    assert js.attempts == 1, "park_poison did not attempt the DLQ publish at all"
    assert parked is False, "a failed DLQ publish must be reported as an unparked unit, not swallowed as success"


@pytest.mark.asyncio
async def test_a_successful_park_says_so() -> None:
    """The other half of the contract, so False actually distinguishes something."""

    class _Ok:
        async def publish(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(seq=1)

    queue = WorkQueue(cast("Any", SimpleNamespace(close=None)), cast("Any", _Ok()))

    assert await queue.park_poison(_task(), "corrupt payload") is True


@pytest.mark.asyncio
async def test_an_unparkable_permanent_failure_is_still_acked_and_still_named() -> None:
    """The permanent path: the run must finish, and its record must say the DLQ copy is missing."""
    queue = _UnparkableQueue()
    worker = Worker(cast("WorkQueue", queue), fetcher=cast("Any", SimpleNamespace(fetch=None)))
    msg = _Msg()
    outcome = ChunkOutcome(chunk_id="c0")

    await worker._refuse(msg, _task(), _permanent(), outcome)

    assert msg.acked, "an unparkable unit was left unacked — the drain stalls and the chunk never completes"
    assert "k" in outcome.errors, "the unit vanished from the run's record when its DLQ copy failed"
    assert "DLQ" in outcome.errors["k"], f"the run's record does not say the evidence copy is missing: {outcome.errors['k']!r}"


@pytest.mark.asyncio
async def test_an_unparkable_exhausted_transient_is_still_acked_and_still_named() -> None:
    """The terminal-transient path, which JetStream would otherwise drop with no record anywhere."""
    queue = _UnparkableQueue()
    worker = Worker(cast("WorkQueue", queue), fetcher=cast("Any", SimpleNamespace(fetch=None)))
    msg = _Msg(num_delivered=MAX_DELIVER)
    outcome = ChunkOutcome(chunk_id="c0")

    await worker._refuse(msg, _task(), _transient(), outcome)

    assert msg.acked, "an unparkable exhausted unit was left unacked — JetStream drops it AND the chunk hangs"
    assert "DLQ" in outcome.errors.get("k", ""), "the exhausted unit's record does not say its DLQ copy is missing"
