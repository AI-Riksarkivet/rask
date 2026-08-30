"""The worker's ONE loop — the property every pooled async resource in an activity depends on.

The bug this seam closes was not theoretical and not found by reading: medallion's stage dispatches
logged `Activity execution failed - task_id: 1, error: Event loop is closed` on EVERY dispatch for as
long as anyone had looked, then succeeded on the retry. The estate looked healthy because the retry
worked. See `activity_loop`'s own docstring for the mechanism.

So the property under test is stated in the terms that were violated: two separate `run_activity`
calls must be able to share loop-bound state, because the production callers do.
"""

from __future__ import annotations

import asyncio

import pytest

from service_kit.activity_loop import run_activity, stop_worker_loop, worker_loop


@pytest.fixture(autouse=True)
def _fresh_loop():
    """Each test gets its own worker loop, and leaves none running behind it."""
    stop_worker_loop()
    yield
    stop_worker_loop()


def test_two_activities_run_on_the_SAME_loop() -> None:
    """The whole contract. Under `asyncio.run` these were two different loops, the first already
    closed by the time the second ran — which is what stranded the pooled HTTP connection."""

    async def which() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert run_activity(which()) is run_activity(which()), "a fresh loop per activity is the bug, not the design"


def test_loop_bound_state_SURVIVES_between_activities() -> None:
    """`asyncio.Lock` binds to the loop that first awaits it and refuses any other — the same class of
    breakage as the httpx pool, reachable without a socket."""
    lock = asyncio.Lock()

    async def guarded() -> bool:
        async with lock:
            return True

    assert run_activity(guarded())
    assert run_activity(guarded()), "the second activity met a lock bound to the first one's loop"


def test_the_loop_is_not_left_running_after_a_stop() -> None:
    """A worker that shuts down must not leave a loop thread holding pooled resources open."""
    loop = worker_loop()
    run_activity(asyncio.sleep(0))
    stop_worker_loop()

    for _ in range(200):
        if not loop.is_running():
            break
        __import__("time").sleep(0.01)
    assert not loop.is_running()


def test_a_stopped_loop_is_REPLACED_rather_than_reused() -> None:
    """Shutdown must not strand a late activity: asking again after a stop builds a fresh loop instead
    of handing back the dead one."""
    first = worker_loop()
    stop_worker_loop()
    assert worker_loop() is not first


def test_an_exception_propagates_to_the_activity_body() -> None:
    """The activity's return value is what Dapr durably records, so a failure must reach it as a
    failure — a bridge that swallowed it would record success for work that never happened."""

    async def boom() -> None:
        raise ValueError("from the worker loop")

    with pytest.raises(ValueError, match="from the worker loop"):
        run_activity(boom())


def test_concurrent_activities_share_one_loop() -> None:
    """The worker runs activities on SEVERAL threads. Two starting together must not build two loops —
    that would pin their pooled clients to different ones and reintroduce the fault sideways."""
    import threading

    seen: list[asyncio.AbstractEventLoop] = []
    barrier = threading.Barrier(8)

    async def which() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def worker() -> None:
        barrier.wait()
        seen.append(run_activity(which()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(seen) == 8
    assert len(set(map(id, seen))) == 1, "a race in the double-checked start produced more than one loop"
