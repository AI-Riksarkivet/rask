"""The ack heartbeat — infra-free, because the property has nothing to do with a live broker.

`test_worker_queue.py` is skipped wholesale without a running NATS, which is right for the tests that
drive a real subscription and wrong for this one: "a held message gets renewed before its ack
expires" is a property of the worker's own loop, and a test that only runs when someone has a broker
up is a test that does not run.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest


# ── the held batch must not have its own work redelivered underneath it ───────


@pytest.mark.asyncio
async def test_HELD_messages_are_heartbeat_so_the_queue_does_not_redeliver_live_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """`msg.in_progress()` was called NOWHERE in the plane.

    A batch holds its messages unacked from first fetch until the fragment is on the store. Against a
    slow or rate-limited source that window exceeds `ACK_WAIT_SECONDS` (300) and JetStream redelivers
    units a LIVE worker is still holding — the same bytes fetched twice against the very endpoint the
    concurrency limit protects, and two workers' fragments overlapping in a way `discover_staged` may
    not resolve. No crash required, which is what makes it worse than the documented overlap: an
    ordinary slow run reaches it.

    Driven by shrinking the interval to milliseconds rather than waiting a minute — the property is
    "a held message gets renewed", not "it gets renewed after exactly 60 s".
    """
    from ingest import worker as worker_mod

    renewed: list[str] = []

    class _Msg:
        """UNHASHABLE, exactly like nats-py's `Msg`.

        The first version of this test used a plain object, which IS hashable — so it passed against
        a worker that stored held messages in a `set()`, while the real `Msg` raised
        `TypeError: unhashable type: 'Msg'` inside the drain activity on the first in-cluster run.
        Dapr reported that as a failed activity, and the run finished COMPLETE with `units_done: 0`
        and nothing committed. A fake that is easier than the real thing tests the fake.
        """

        __hash__ = None  # type: ignore[assignment]

        def __init__(self, name: str) -> None:
            self.name = name

        async def in_progress(self) -> None:
            renewed.append(self.name)

    monkeypatch.setattr(worker_mod, "HEARTBEAT_SECONDS", 0.01)

    # Drives the PRODUCTION function, not a copy of its loop. The first version of this test
    # reimplemented the loop inline, so it asserted that a heartbeat written in the test worked —
    # which stayed green while the real one raised on an unhashable message.
    held: dict[int, Any] = {id(m): m for m in (_Msg("u0"), _Msg("u1"))}

    beat = asyncio.create_task(worker_mod._renew_held(held))
    await asyncio.sleep(0.05)
    beat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await beat

    assert renewed, "a held message was never renewed — the queue will redeliver it out from under the worker"
    assert {"u0", "u1"} <= set(renewed)


def test_the_heartbeat_interval_is_DERIVED_from_the_ack_wait() -> None:
    """A literal here would drift silently against `queue.ACK_WAIT_SECONDS`.

    The only symptom of drift is the queue redelivering live work, which reads as a slow source and
    lands as a staging overlap — never as "the heartbeat is misconfigured". Several consecutive
    misses must still not expire the ack.
    """
    from ingest.queue import ACK_WAIT_SECONDS
    from ingest.worker import HEARTBEAT_SECONDS

    assert HEARTBEAT_SECONDS < ACK_WAIT_SECONDS / 2, "one missed beat must not be able to expire the ack"
    assert ACK_WAIT_SECONDS / HEARTBEAT_SECONDS >= 4, "too few tolerated misses to survive a GC pause or a slow write"
