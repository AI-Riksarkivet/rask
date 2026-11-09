"""Maintenance's replicas share one work queue, and a dead replica's task goes to another ([[LH-494]] closes-when (b),
ADR 0198 §2).

The service's own worker (`maintenance.services.queue.Worker` over `TaskQueue`) on the broker the bus lane stands up: the
NATS fixture's nats-server in operator mode, the chart's stream Job run as `admin`, which declares MAINTENANCE_TASKS,
MAINTENANCE_SCHEDULES and their two durables, and every worker connected as `maintenance-worker` with the grants the dev
seed issues, so a call the chart does not grant fails here as it would on the estate. Only what a task runs is the
test's: it records which worker ran it, and holds while the test holds it. The render shortens the consumer's ack wait to
`_ACK_WAIT` seconds, so a dead worker's task moves within the test.

* A worker in a process of its own takes a task and is killed mid-run (SIGKILL). While it lived, its heartbeat kept the
  task from the other worker for three ack waits, and the queue refused a second task for the table; once it was dead,
  the task went to the other worker.
* A pass that runs past twice its bound ends its worker's process (exit 70): two workers in processes of their own, with
  a one-second bound, take one table's task in turn and hang on it. The task outlived its worker on both of its
  `maintenance.queue.maxDeliver` deliveries, so the next delivery ends it without running it, which frees its table and
  holds its next try a recheck out.
* A burst of three commits on each of six tables holds one follow-up per table, the other two refused; each fires once,
  and the two ready workers share the six passes, each run once. A worker that is not ready takes none.
* A follow-up that fires while a task holds its table waits until that task is done, then passes the table again: the
  commit it follows may be one the running pass read the table before.

Run by `dagger call nats-auth` (`make nats-auth`), beside the bus's permission proof, whose rig it shares.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from nats_auth_rig import VALUES_ENV, Broker, Credential, Permissions, Tools, Violation, mint, run_stream_job, violations

from maintenance.services.queue import Task, TaskQueue, Worker
from tests.unit.chart_render import DEFAULT_ARGS, nats_users, render


pytestmark = pytest.mark.nats_auth

REPO = Path(__file__).resolve().parents[2]
_WORKER = "maintenance-worker"
#: The consumer's ack wait the render is given, and the heartbeat each worker beats inside it.
_ACK_WAIT = 4
_HEARTBEAT = 1
#: The longest a fired follow-up waits for the task holding its table before it is moved again
#: (`maintenance.services.queue`'s retry, 60 s), with room for the move and the pass.
_FOLLOW_UP_WAIT = 75.0
#: Deliveries of one task before a worker ends it; the consumer allows one more, on which the task is ended unrun.
_MAX_DELIVER = 2
#: The exit a worker ends its process with once a pass runs past twice its bound (`maintenance.services.queue`).
_HUNG_EXIT = 70
#: A worker in a process of its own, its pass bound the third argument: it says which task it runs, then holds it until
#: it is killed or its bound ends the process.
_DOOMED = f"""
import asyncio, sys
from pathlib import Path
from maintenance.services.queue import TaskQueue, Worker

async def main(url: str, creds: str, max_pass: float) -> None:
    queue = await TaskQueue.connect(url, Path(creds).read_text(), name="doomed")

    async def run(task) -> None:
        print("running", task.uuid, flush=True)
        await asyncio.Event().wait()

    await Worker(queue, run=run, ready=lambda: True, heartbeat_seconds={_HEARTBEAT}, max_deliver={_MAX_DELIVER}, max_pass_seconds=max_pass, retry_ended_seconds=3600).run_tasks()

asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3])))
"""


class _Runs:
    """Which worker ran which table's task, in order. A table in `held` holds its run until the test sets its event."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, str]] = []
        self.held: dict[str, asyncio.Event] = {}

    def of(self, worker: str) -> Callable[[Task], Awaitable[None]]:
        async def run(task: Task) -> None:
            self.ran.append((worker, task.uuid))
            gate = self.held.get(task.uuid)
            await (gate.wait() if gate is not None else asyncio.sleep(1.0))

        return run

    def times(self, uuid: str) -> int:
        return sum(1 for _, ran in self.ran if ran == uuid)


class _Replica:
    """One worker as a replica runs it: a connection of its own as `maintenance-worker`, and both of its loops."""

    def __init__(self, queue: TaskQueue, worker: Worker) -> None:
        self._queue, self._worker = queue, worker
        self._loops = [asyncio.create_task(worker.run_tasks()), asyncio.create_task(worker.run_due())]

    @classmethod
    async def start(cls, url: str, credential: Credential, name: str, runs: _Runs, *, ready: Callable[[], bool]) -> _Replica:
        queue = await TaskQueue.connect(url, credential.creds_file.read_text(), name=name)
        return cls(
            queue,
            Worker(
                queue, run=runs.of(name), ready=ready, heartbeat_seconds=_HEARTBEAT, max_deliver=_MAX_DELIVER, max_pass_seconds=600, retry_ended_seconds=3600
            ),
        )

    async def stop(self) -> None:
        self._worker.stop()
        for loop in self._loops:
            loop.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        await self._queue.close()


async def _doomed(url: str, credential: Credential, max_pass_seconds: float) -> asyncio.subprocess.Process:
    """A worker in a process of its own, its pass bound ``max_pass_seconds``."""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", _DOOMED, url, str(credential.creds_file), str(max_pass_seconds), cwd=REPO, stdout=asyncio.subprocess.PIPE
    )


async def _said(process: asyncio.subprocess.Process) -> list[str]:
    """The next line a worker in a process of its own printed, as words."""
    assert process.stdout is not None
    return (await asyncio.wait_for(process.stdout.readline(), 60)).decode().split()


async def _until(done: Callable[[], bool | Awaitable[bool]], seconds: float) -> bool:
    """Whether ``done`` came true within ``seconds``, asked every 200 ms."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        outcome = done()
        if await outcome if inspect.isawaitable(outcome) else outcome:
            return True
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.2)


def _declare_the_queue(tools: Tools, docs: tuple[dict[str, Any], ...], broker: Broker, admin: Credential, home: Path) -> None:
    """The chart's stream Job, run as `admin` against this broker, as the estate runs it before maintenance starts."""
    job = next(d for d in docs if d.get("kind") == "Job" and "-nats-stream-" in d["metadata"]["name"])
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    rendered = re.search(r'S="(nats://[^"]+)"', script)
    assert rendered, "the stream Job's script names no broker"
    done = run_stream_job(tools.job_cli, script.replace(rendered.group(1), broker.url), admin, home, 300.0)
    assert done.returncode == 0, f"the stream Job exited {done.returncode}: {(done.stdout + done.stderr)[-800:]}"


@pytest.mark.asyncio
async def test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other(tmp_path: Path) -> None:
    tools = Tools.from_env(os.environ)
    values = Path(os.environ.get(VALUES_ENV) or REPO / "chart" / "values.yaml")
    queue_values = (
        *("--set", f"maintenance.queue.ackWaitSeconds={_ACK_WAIT}", "--set", f"maintenance.queue.heartbeatSeconds={_HEARTBEAT}"),
        *("--set", f"maintenance.queue.maxDeliver={_MAX_DELIVER}"),
    )
    docs = render(*DEFAULT_ARGS, "-f", str(values), "--set", "nats.streamReplicas=1", *queue_values)
    issued = nats_users(docs)
    table = {
        user: Permissions(publish=tuple(sorted(issued[user][0])), subscribe=tuple(sorted(issued[user][1]))) for user in ("admin", _WORKER) if user in issued
    }
    assert set(table) == {"admin", _WORKER}, f"the render with {values} issues no {sorted({'admin', _WORKER} - set(table))}"
    mint_ = mint(tools.nsc, table, tmp_path / "nsc")
    worker = mint_.credentials[_WORKER]
    broker = Broker(tools.nats_server, mint_, tmp_path / "broker")
    broker.start()
    runs = _Runs()
    replicas: list[_Replica] = []
    doomed: list[asyncio.subprocess.Process] = []
    lane: TaskQueue | None = None

    def refusals() -> list[Violation]:
        """What the broker refused the worker's user: a call the chart's table does not grant."""
        return [v for v in violations(broker.log_text()) if mint_.user_of(v.user_key) == _WORKER]

    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        # The commit lane's connection: the service's own publishes, under the same user.
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")

        killer = Task(kind="table", uuid="killer", location="s3://bucket/killer")
        assert await lane.enqueue(killer) is True, refusals()
        for _ in range(_MAX_DELIVER):
            doomed.append(hung := await _doomed(broker.url, worker, 1))
            assert await _said(hung) == ["running", "killer"], f"a worker did not take the task its table held; refused: {refusals()}"
            assert await asyncio.wait_for(hung.wait(), 30) == _HUNG_EXIT, "a pass past twice its bound did not end its worker's process"

        dying = Task(kind="table", uuid="dying", location="s3://bucket/dying")
        doomed.append(dier := await _doomed(broker.url, worker, 600))
        assert await lane.enqueue(dying) is True, refusals()
        said = await _said(dier)
        assert said == ["running", "dying"], f"the doomed worker said {said}; refused: {refusals()}"
        replicas.append(await _Replica.start(broker.url, worker, "second", runs, ready=lambda: True))
        assert await lane.enqueue(dying) is False, "the queue took a second task for a table whose task is in flight"
        # Three ack waits in which the task must stay with the worker that still beats for it: nothing to poll for.
        await asyncio.sleep(3 * _ACK_WAIT)
        assert runs.times("dying") == 0, "a live worker's task went to another: its heartbeat did not hold it"
        dier.kill()
        await dier.wait()
        assert await _until(lambda: runs.times("dying") == 1, _ACK_WAIT + 15), f"a dead worker's task never reached the other; refused: {refusals()}"

        async def ended() -> bool:
            return "killer" not in await lane.held("task", "table")

        assert await _until(ended, _ACK_WAIT + 15), "a task that outlived its worker on every delivery still holds its table"
        assert runs.times("killer") == 0, "a task past its last delivery was run, not ended"
        assert "killer" in await lane.held("schedule", "table"), "an ended task's table holds no next try, so the hourly audit hands it back"

        replicas.append(await _Replica.start(broker.url, worker, "first", runs, ready=lambda: True))
        replicas.append(await _Replica.start(broker.url, worker, "not-ready", runs, ready=lambda: False))
        burst = [Task(kind="table", uuid=f"burst-{n}", location=f"s3://bucket/burst-{n}") for n in range(6)]
        due = datetime.now(UTC) + timedelta(seconds=2)
        held = [[await lane.follow_up(task, due) for _ in range(3)] for task in burst]
        assert held == [[True, False, False]] * len(burst), f"a burst held other than one follow-up per table: {held}"
        assert await _until(lambda: all(runs.times(task.uuid) for task in burst), 30), f"the burst's passes did not all run: {runs.ran}; refused: {refusals()}"
        # An ack wait past the last pass, in which no pass may run a second time: nothing to poll for.
        await asyncio.sleep(_ACK_WAIT + 1)
        passes = [(name, uuid) for name, uuid in runs.ran if uuid.startswith("burst-")]
        assert Counter(uuid for _, uuid in passes) == Counter(task.uuid for task in burst), f"a burst's pass ran other than once: {passes}"
        assert {name for name, _ in passes} == {"first", "second"}, f"the two ready workers did not share the burst, or one not ready took a pass: {passes}"

        busy = Task(kind="table", uuid="busy", location="s3://bucket/busy")
        release = runs.held["busy"] = asyncio.Event()
        assert await lane.enqueue(busy) is True
        assert await _until(lambda: runs.times("busy") == 1, 15), f"the busy table's task never ran; refused: {refusals()}"
        assert await lane.follow_up(busy, datetime.now(UTC) + timedelta(seconds=1)) is True
        # The follow-up fires inside these seconds while the task still holds its table, and must not pass it: nothing to poll for.
        await asyncio.sleep(4)
        assert runs.times("busy") == 1, "a follow-up was passed while a task still held its table"
        release.set()
        assert await _until(lambda: runs.times("busy") == 2, _FOLLOW_UP_WAIT), "a follow-up that fired while its table was held was never passed"

        async def drained() -> bool:
            return not await lane.held("task", "table")

        assert await _until(drained, 10), f"the queue still holds {await lane.held('task', 'table')} after every task ran"
    finally:
        for process in doomed:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for replica in replicas:
            await replica.stop()
        if lane is not None:
            await lane.close()
        broker.stop()
    assert refusals() == [], "the broker refused the worker a call the chart must grant"
