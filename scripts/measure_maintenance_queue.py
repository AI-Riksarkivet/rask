"""What maintenance's work queue costs: tasks per second, a task's wait and the queue's own time per task, at 1, 2 and 4
workers (ADR 0198's amendment of 2026-11-13, [[LH-571]]).

The bus is the nats-auth lane's (`tests/e2e-py/nats_auth_rig.py`): three nats-servers in operator mode, its signers, the
streams the chart's stream Job declares at the chart's default timings, and the worker's own bus user, each connection
presenting a pod's token. A burst of tasks, each for a table of its own, is queued first; then the workers start, each on
a connection of its own, in this one process, and run every task as a sleep of the stated length. A task's wait is
counted from the workers' start to its run's start.

Run, with the lane's binaries exported as `dagger call nats-auth` lays them out (`.dagger/nats_auth.go`):

    RASK_NATS_AUTH_TOOLS=<dir> PYTHONPATH=tests/e2e-py:. uv run python scripts/measure_maintenance_queue.py out.json
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Final, NoReturn

from nats_auth_rig import RENDER_ARGS, VALUES_ENV, Rig, Tools, bus_users, stream_job_script
from pydantic import BaseModel

from maintenance.services.queue import TASKS_STREAM, Task, TaskQueue, Worker
from tests.chart.chart_render import DEFAULT_ARGS, render


REPO: Final = Path(__file__).resolve().parents[1]
_WORKER: Final = "maintenance-worker"
#: Each case: the pass's length in seconds and the burst's size, run at every worker count, `_RUNS` times.
_CASES: Final = ((0.0, 200), (0.5, 80))
_WORKERS: Final = (1, 2, 4)
_RUNS: Final = 3
#: The chart's defaults (`maintenance.queue` in `chart/values.yaml`), which the render declares the consumer with.
_HEARTBEAT: Final = 20.0
_ACK_WAIT: Final = 60.0
_MAX_DELIVER: Final = 10


class Measurement(BaseModel):
    """One burst drained by ``workers`` workers."""

    workers: int
    tasks: int
    pass_seconds: float
    run: int
    drain_seconds: float
    tasks_per_second: float
    overhead_ms_per_task: float
    wait_median_seconds: float
    wait_p95_seconds: float


def _declare(bus: Rig, docs: tuple[dict[str, Any], ...], home: Path) -> None:
    """Run the chart's stream Job against the rig's bus as `admin`, again while it fails, as Kubernetes runs a failed Job."""
    script = stream_job_script(docs, bus.url)
    for _ in range(3):
        done = bus.run_stream_job(script, home, 300.0)
        if done.returncode == 0:
            return
    raise RuntimeError(f"the stream Job exited {done.returncode}: {(done.stdout + done.stderr)[-800:]}")


async def _connected(bus: Rig) -> TaskQueue:
    """A worker's queue connection, presenting the token of a pod of its own."""
    return await TaskQueue.connect(bus.url, bus.pod(_WORKER).settings(bus.url, bus.table.session_seconds), name=_WORKER)


async def _drain(bus: Rig, *, workers: int, tasks: int, pass_seconds: float, tag: str, run: int) -> Measurement:
    clock = asyncio.get_running_loop().time
    started: dict[str, float] = {}
    ended: set[str] = set()

    async def sleeps(task: Task) -> None:
        started[task.uuid] = clock()
        await asyncio.sleep(pass_seconds)
        ended.add(task.uuid)

    def ends_its_process(event: str, **extra: object) -> NoReturn:
        raise RuntimeError(f"a worker ended its process: {event} {extra}")

    feeder = await _connected(bus)
    for i in range(tasks):
        if not await feeder.enqueue(Task(kind="table", uuid=f"{tag}-{i}", location=f"s3://bucket/{tag}-{i}")):
            raise RuntimeError(f"the queue refused {tag}-{i}")
    queues = [await _connected(bus) for _ in range(workers)]
    fleet = [
        Worker(
            queue,
            run=sleeps,
            ready=lambda: True,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=_MAX_DELIVER,
            max_pass_seconds=1800,
            retry_ended_seconds=3600,
            end=ends_its_process,
        )
        for queue in queues
    ]
    began = clock()
    loops = [asyncio.create_task(worker.run_tasks()) for worker in fleet]
    while len(ended) < tasks or (await feeder.stream_state(TASKS_STREAM)).messages:
        for loop in loops:
            if loop.done():
                loop.result()
        await asyncio.sleep(0.02)
    drained = clock() - began
    for worker in fleet:
        worker.stop()
    await asyncio.gather(*loops, return_exceptions=True)
    for queue in (*queues, feeder):
        await queue.close()
    waits = sorted(at - began for at in started.values())
    return Measurement(
        workers=workers,
        tasks=tasks,
        pass_seconds=pass_seconds,
        run=run,
        drain_seconds=round(drained, 2),
        tasks_per_second=round(tasks / drained, 2),
        overhead_ms_per_task=round(1000 * (drained * workers / tasks - pass_seconds), 1),
        wait_median_seconds=round(statistics.median(waits), 2),
        wait_p95_seconds=round(waits[round(0.95 * (len(waits) - 1))], 2),
    )


async def main(out: Path) -> None:
    """Measure every case at every worker count on a bus of this run's own, and write the measurements to ``out``."""
    home = Path(tempfile.mkdtemp(prefix="maintenance-queue-"))
    tools = Tools.from_env(os.environ)
    docs = render(*DEFAULT_ARGS, "-f", os.environ.get(VALUES_ENV) or str(REPO / "chart" / "values.yaml"), *RENDER_ARGS)
    bus = Rig(tools, docs, home / "rig", session_seconds=bus_users(docs)[0].session_seconds)
    bus.start()
    measured: list[Measurement] = []
    try:
        await asyncio.to_thread(_declare, bus, docs, home)
        for pass_seconds, tasks in _CASES:
            for workers in _WORKERS:
                for run in range(1, _RUNS + 1):
                    measured.append(await _drain(bus, workers=workers, tasks=tasks, pass_seconds=pass_seconds, tag=f"b{len(measured)}", run=run))
                    sys.stdout.write(measured[-1].model_dump_json() + "\n")
    finally:
        bus.stop()
    out.write_text(json.dumps([m.model_dump() for m in measured], indent=1))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
