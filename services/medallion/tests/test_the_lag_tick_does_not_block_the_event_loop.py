"""The lag tick's blocking reads run OFF the event loop, so the pod stays probeable while it ticks.

`run_lag_tick` is pure over two readers, and both readers are SYNCHRONOUS `httpx.get` calls
(`cascade_lag_readers.py:144,188`) issued once per declared edge. On the deployed estate that is one
catalog round-trip per governed tenant plus one lineage read each — measured 2026-09-06 on
`rask-medallion-producer`, a single tick spanned 21:01:40 to 21:01:55, fifteen seconds of sequential
blocking IO.

An `async def` door that calls it directly does that IO ON THE EVENT LOOP, and nothing else the
process owes an answer to gets one for the duration. The probes are the first to notice: k8s gives
them `timeoutSeconds: 1`, so the same pod logged 21 `Readiness probe failed: context deadline
exceeded` and 7 of the LIVENESS probe in 22 minutes. Two consequences, and the second is the worse:

  * NotReady drops the pod from the `rask-medallion-producer` Service's endpoints, so callers get
    "not reachable" against a healthy producer — that is what made `test_governed_union_e2e` skip
    all five legs on one run and execute them on the next, from one estate, minutes apart.
  * Three consecutive liveness failures RESTART the container. Seven in 22 minutes is that margin
    nearly spent, and the restart would land mid-cascade on a service whose whole job is durable work.

The tick is not too slow — it is in the wrong place. `run_in_threadpool` is what the rest of this
service already does with blocking work (`media_produce.py` wraps `register_written_dataset` in it).

THE ASSERTION IS THE THREAD, AND A PROBE-RESPONSIVENESS TEST WAS TRIED AND DISCARDED. The obvious
shape — hold the tick, ask `/livez`, fail on a timeout — cannot detect this defect: a blocked loop
runs no timers either, so `asyncio.wait_for` does not fire while the loop is held and both the answer
and its own deadline come due together once it is free. That test PASSED against the blocking door.
"Blocking work is off the loop" is the property, the thread identity states it exactly, and a duration
threshold would only be a proxy a loaded CI box could fail on its own.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from medallion.api import cascade_lag_cron
from medallion.services.cascade_lag import LagTickReport
from service_kit.lakehouse.ns_errors import install_problem_handlers


BINDING = "medallion-cascade-lag-cron"
#: A real tick's blocking IO measured fifteen seconds; this only has to be long enough to be an
#: unmistakable stand-in for it, and short enough that the suite does not pay for the demonstration.
TICK_BLOCK_SECONDS = 0.2


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, list[int]]:
    """The cron door, with both stores stubbed and the tick replaced by a BLOCKING spy.

    The readers are stubbed at their own module because `_on_cron` imports them inside the function —
    the lazy import is what keeps the engine's readers out of the import graph, and patching the
    door's namespace would miss them entirely and issue real catalog calls.
    """
    ticked_on: list[int] = []

    def _blocking_tick(**_kwargs: Any) -> LagTickReport:
        ticked_on.append(threading.get_ident())
        time.sleep(TICK_BLOCK_SECONDS)  # stands in for the sequential per-edge `httpx.get` calls
        return LagTickReport(edges=0, published_points=0, unknown=0, failed=0)

    monkeypatch.setattr(cascade_lag_cron, "run_lag_tick", _blocking_tick)
    monkeypatch.setattr("medallion.services.cascade_lag_readers.declared_edges", lambda _s: [])
    monkeypatch.setattr("medallion.services.cascade_lag_readers.published_reader", lambda _s: lambda e, p: None)
    monkeypatch.setattr("medallion.services.cascade_lag_readers.consumed_reader", lambda _s: lambda e, p: [])

    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    cascade_lag_cron.mount_lag_cron(application, BINDING)

    return application, ticked_on


@pytest.mark.asyncio
async def test_the_tick_runs_off_the_event_loop_thread(app: tuple[FastAPI, list[int]]) -> None:
    application, ticked_on = app
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post(f"/{BINDING}", timeout=30)

    assert ticked_on, "the tick never ran"
    assert ticked_on[0] != threading.get_ident(), (
        "the lag tick ran on the event loop's own thread — its readers are blocking `httpx.get` calls, "
        "one per declared edge, so every other request this process owes an answer to waits for the "
        "whole scan. Wrap it in `run_in_threadpool`."
    )
