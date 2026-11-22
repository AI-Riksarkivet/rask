"""How much of the request thread pool a lakehouse process is using, exported as gauges.

Every sync door, and every blocking call a door hands to ``run_in_threadpool`` (a store read, a Lance commit, a key
fetch), runs on a thread under anyio's default limiter. The limiter is bounded (40 threads), so once every thread is
held each further call waits for one while its request holds its place: latency rises with no error anywhere. The three
gauges say so before the latency does: the threads in use, the limit, and the calls waiting for a thread. What a
process runs on thread sets instead (`lakehouse.process.own_threads`: its own renewals, timers and readiness reads, the
commit turns' reads, lineage's calls through the catalog) is apart from this pool and not in the gauges.

Observable gauges, read at each export from the limiter of the loop that called :func:`watch`. Only that process
reports them: one that never serves (a script importing the library) exports no constant zero.
"""

from __future__ import annotations

from anyio import CapacityLimiter, to_thread
from opentelemetry import metrics


_meter = metrics.get_meter("lance.app")

#: The limiter of the loop that last called :func:`watch`: the one its requests' blocking calls borrow from.
_limiter: CapacityLimiter | None = None
_registered = False


def _busy(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(_limiter.borrowed_tokens)] if _limiter is not None else []


def _limit(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(_limiter.total_tokens)] if _limiter is not None else []


def _waiting(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    return [metrics.Observation(_limiter.statistics().tasks_waiting)] if _limiter is not None else []


def watch() -> None:
    """Report the calling event loop's default thread pool from now on. Called inside the loop, when the app starts."""
    global _limiter, _registered
    _limiter = to_thread.current_default_thread_limiter()
    if _registered:
        return
    _registered = True
    _meter.create_observable_gauge(
        "threadpool.busy",
        callbacks=[_busy],
        unit="{thread}",
        description="Threads of the request thread pool running a blocking call now.",
    )
    _meter.create_observable_gauge(
        "threadpool.limit",
        callbacks=[_limit],
        unit="{thread}",
        description="The most threads the request thread pool runs at once.",
    )
    _meter.create_observable_gauge(
        "threadpool.waiting",
        callbacks=[_waiting],
        unit="{call}",
        description="Blocking calls waiting for a thread of the request thread pool: above 0, every thread is held and requests queue.",
    )


__all__ = ["watch"]
