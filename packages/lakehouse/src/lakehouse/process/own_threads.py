"""Worker threads a lakehouse process bounds apart from the request pool its doors hold.

A sync door, and whatever a door hands to ``run_in_threadpool``, runs under anyio's default limiter (40 threads). Work
that must not wait behind those doors, or that must hold none of their threads while it waits, runs on a
:class:`ThreadSet`: an executor of the set's own, so a burst that fills the request pool, or one set, queues nothing in
another, and none of it runs on the event loop's default executor, where asyncio resolves names.

The process's own set (:func:`on_own_threads`) carries what lets a process recover and what its loops do: its store
session's exchange and renewal, its secret store's login, its keys' resolves, its OIDC refresh, its timers' window claims
and its readiness reads. Work that callers drive has a set of its own, so no burst of them queues that recovery: the
commit turns' reads (`lakehouse.records.commit_turns`) and lineage's calls through the catalog.

A caller that is cancelled stops waiting at once, so a budget or a shutdown is not held until the call returns. A call
that has started runs on and keeps its seat until it returns, and one still waiting for a seat is dropped, so a set
never runs more threads than it seats, whatever is cancelled, and a call that hangs holds a seat for as long as it
does: a call given to a set should be bounded by a timeout of its own.
"""

import asyncio
import contextvars
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Final


class ThreadSet:
    """A bounded set of worker threads, apart from the request pool and from every other set, named
    ``lakehouse-<name>_<n>``, which is how a thread dump tells the sets apart."""

    #: How many of a set's calls run at once, sized as asyncio sizes its default executor
    #: (``min(32, os.process_cpu_count() + 4)``); a further call waits its turn, holding no thread.
    seats: Final = min(32, (os.process_cpu_count() or 1) + 4)

    def __init__(self, name: str) -> None:
        self._threads = ThreadPoolExecutor(max_workers=self.seats, thread_name_prefix=f"lakehouse-{name}")

    async def run[*Ts, R](self, call: Callable[[*Ts], R], *args: *Ts) -> R:
        """``call(*args)``, a blocking call, on a thread of this set, under the caller's context variables."""
        in_context = partial(contextvars.copy_context().run, call, *args)
        return await asyncio.get_running_loop().run_in_executor(self._threads, in_context)


_OWN: Final = ThreadSet("own")


async def on_own_threads[*Ts, R](call: Callable[[*Ts], R], *args: *Ts) -> R:
    """``call(*args)``, a blocking call of the process's own, on a thread of the process's own set."""
    return await _OWN.run(call, *args)


__all__ = ["ThreadSet", "on_own_threads"]
