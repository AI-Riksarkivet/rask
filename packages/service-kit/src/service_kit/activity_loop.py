"""One event loop for a durable worker's SYNC activity bodies.

Dapr Workflow activities are sync callables executed on the durable-task worker's threads, while
almost everything they need to call — the Dapr client, `httpx`, `nats-py` — is async. Every service
bridged that gap the same way: `asyncio.run(coro)`, a fresh loop per activity, closed on the way out.

**That is correct only for an activity that shares NO state with the next one, and the estate's
activities do.** `medallion.services.ray_submit` pools one `httpx.AsyncClient` for "the worker's
lifetime", deliberately and with a test — but a pooled keep-alive connection belongs to the loop that
opened it, so under `asyncio.run` the next activity inherited a connection bound to a loop that no
longer exists. Measured live 2026-08-30: EVERY medallion stage dispatch logged
``Activity execution failed - task_id: 1, error: Event loop is closed`` and then succeeded on the
retry ~2 s later, because the failed attempt evicted the dead connection. The cascade completed every
time, which is why it survived so long — the cost was one wasted retry per stage and a warning that
read like the SDK's own noise.

The two designs that are self-consistent are "a loop per activity AND a client per activity" or
"a loop per worker AND a client per worker". This module is the second, because the first throws away
a TCP connect and a TLS handshake on every activity of a workflow built to run them repeatedly.

**Why a thread rather than a loop this thread runs:** the caller IS a sync activity body, so it must
block until the coroutine finishes. A loop cannot be run re-entrantly from inside itself, and the
worker may call activities concurrently on several threads. One long-lived loop on its own thread,
fed with `run_coroutine_threadsafe`, is the only shape that serves both — and it gives every pooled
async resource in the process exactly one loop to be bound to.

The loop is a daemon thread: it must never hold up interpreter exit, and a worker that is shutting
down has nothing left to run on it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Coroutine

#: The worker's loop, built on first use. Module-level for the same reason the clients that depend on
#: it are: an activity has no `Request` and no reachable `app.state`, so there is nowhere else it can
#: live that every activity can reach.
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def worker_loop() -> asyncio.AbstractEventLoop:
    """The process's activity loop, started on first use.

    Double-checked, and the check is not ceremony: the worker runs activities on several threads, so
    two starting together would otherwise build two loops and pin their pooled clients to different
    ones — reintroducing the exact cross-loop bug from the other direction.
    """
    global _loop
    running = _loop
    if running is not None and not running.is_closed():
        return running
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            fresh = asyncio.new_event_loop()
            # `run_forever` blocks its thread until `stop()`; work arrives via `call_soon_threadsafe`,
            # which queues fine even in the moment before the loop is running.
            threading.Thread(target=fresh.run_forever, name="service-kit-activity-loop", daemon=True).start()
            _loop = fresh
        return _loop


def run_activity[T](coro: Coroutine[object, object, T]) -> T:
    """Run one coroutine on the worker loop and block until it returns.

    Blocking is the contract, not a compromise: the caller is a sync activity body whose return value
    Dapr durably records, so it cannot yield.
    """
    return asyncio.run_coroutine_threadsafe(coro, worker_loop()).result()


def stop_worker_loop() -> None:
    """Stop and close the worker loop. Idempotent, and safe to call when none was ever started.

    For the SHUTDOWN path and for tests. Anything that runs afterwards gets a new loop rather than a
    closed one, so this cannot strand a late activity — it only guarantees the current loop is not
    left running with pooled resources on it.
    """
    global _loop
    with _loop_lock:
        loop = _loop
        _loop = None
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(loop.stop)
