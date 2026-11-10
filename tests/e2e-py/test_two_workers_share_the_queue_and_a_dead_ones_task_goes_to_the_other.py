"""Maintenance's replicas share one work queue, and a dead replica's task goes to another ([[LH-494]] closes-when (b),
ADR 0198 §2).

The service's own worker (`maintenance.services.queue.Worker` over `TaskQueue`) on the bus the lane stands up: the NATS
fixture's three-node nats-server cluster in operator mode, the chart's stream Job run as `admin`, which declares
MAINTENANCE_TASKS, MAINTENANCE_SCHEDULES and their two durables at the chart's default replicas, a copy on every server as
on the estate, and every worker connected as `maintenance-worker` with the grants the dev seed issues, so a call the chart
does not grant fails here as it would on the estate. Only what a task runs is the
test's: it records which worker ran it, and holds while the test holds it. The render shortens the consumer's ack wait to
`_ACK_WAIT` seconds, so a dead worker's task moves within the test.

* A worker in a process of its own takes a task late in its pull, as one put on an idle queue arrives, and is killed
  mid-run (SIGKILL). Its lease counts from the task's arrival, so while it lived its heartbeat kept the
  task from the other worker for three ack waits, and the queue refused a second task for the table; once it was dead,
  the task went to the other worker.
* A pass that runs past twice its bound ends its worker's process (exit 70): two workers in processes of their own, with
  a one-second bound, take one table's task in turn and hang on it. The task outlived its worker on both of its
  `maintenance.queue.maxDeliver` deliveries, so the next delivery ends it without running it, which frees its table and
  holds its next try a recheck out.
* A worker the bus stops answering mid-pass (its connection cut through a `Link`) ends its process (exit 70) before the
  ack wait its last answered heartbeat restarted runs out, so its task, handed to the other worker, starts only once the
  first has ended: never two passes of one table at once.
* A worker asked to stop whose drain ends with a pass still running ends its process (exit 70), so the task, handed to
  the other worker after its ack wait, never runs beside it.
* A worker whose pass ends while the bus is cut ends its process (exit 70) once its ack cannot be answered before its
  lease runs out, so no ack held through the cut lands after it: the task, handed to the other worker, stays held.
* A task JetStream delivers to a pull its worker gave up on (one replayed, as a reconnect replays it, while the worker's
  pass runs) is never run off that delivery: it waits out its ack wait and runs once, on the worker that pulled it next,
  even after the first worker's pass ends and it pulls again.
* An ack the network held past its worker's lease lands on a delivery a live pass holds (the rig sends it, as a
  partition that heals would), freeing the table's task under that pass. A task put for the table then waits out the
  live pass's claim on it, and runs only once that pass is done: never two passes of one table at once.
* A burst of three commits on each of six tables, their follow-ups put at once as replicas taking a burst of store events
  put them, holds one follow-up per table, the other two refused; each fires once, and the two ready workers share the
  six passes, each run once. A worker that is not ready takes none. A copied stream refuses a put racing another for its
  subject with another code than a put after it (`nats_auth_rig.Broker`), and both mean the table is covered.
* Three puts of one table's task at once queue one. A follow-up that fires while that task holds its table waits until
  the task is done, then passes the table again: the commit it follows may be one the running pass read the table before.

A replica started as the service starts it (`maintenance.services.tasks.running_worker`, its bus credential read from a
stand-in of the store's secrets API) keeps its loops and its schedules:

* A loop that ends with an error runs again after a pause, so a follow-up the failed loop would have moved still runs.
* The audit's and the scan's schedules, which a purged or recreated schedules stream loses, are put back on the cadence
  the replica asserts them on.

A worker whose credential the bus takes no longer ends its process (exit 70), whether the bus ends the session holding it
or refuses it at a reconnect: the store issues a new credential when a user's grants change and revokes the old one, and
the worker, started again, reads the new one.

Run by `dagger call nats-auth` (`make nats-auth`), beside the bus's permission proof, whose rig it shares.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
import respx
from nats.aio.msg import Msg
from nats_auth_rig import VALUES_ENV, Broker, Client, Credential, Link, Mint, Permissions, Tools, Violation, issue, mint, run_stream_job, violations
from pydantic import SecretStr

from lakehouse import location_claims, store_session
from lakehouse.control_emit import NoopControlEmitter
from lakehouse.governed.store_credential import S3KeyPair
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import queue as worker_queue
from maintenance.services import tasks
from maintenance.services.queue import INBOX_PREFIX, SCHEDULES_STREAM, STORE_EVENTS_STREAM, TASK_CONSUMER, TASKS_STREAM, Task, TaskQueue, Worker
from maintenance.services.store_events import StoreEvents
from tests.unit.chart_render import DEFAULT_ARGS, nats_users, render


pytestmark = pytest.mark.nats_auth

REPO = Path(__file__).resolve().parents[2]
_WORKER = "maintenance-worker"
#: The store's own bus user, which publishes each object write on `store.events` (`deploy/fixtures/rustfs`).
_STORE = "object-store"
#: The test's own user, every JetStream call granted: it reads and purges what the worker's schedules leave on the bus,
#: and sees and replays the workers' pulls.
_RIG = "rask-test-rig"
_RIG_GRANTS = Permissions(publish=("$JS.API.>", "$JS.ACK.>"), subscribe=("_INBOX.>", "$JS.API.CONSUMER.MSG.NEXT.>", f"{INBOX_PREFIX}.>"))
#: The consumer's ack wait the render is given, and the heartbeat each worker beats inside it.
_ACK_WAIT = 4
_HEARTBEAT = 1
#: The longest a fired follow-up waits for the task holding its table before it is moved again
#: (`maintenance.services.queue`'s retry, 60 s), with room for the move and the pass.
_FOLLOW_UP_WAIT = 75.0
#: Deliveries of one task before a worker ends it; the consumer allows one more, on which the task is ended unrun.
_MAX_DELIVER = 2
#: The exit a worker ends its process with once a pass runs past twice its bound, once the bus stops answering its
#: heartbeats until the ack wait runs out, or once its drain ends with a pass still running (`maintenance.services.queue`).
_HUNG_EXIT = 70
#: How many times the stream Job runs before the lane gives up on it, inside the chart's `backoffLimit` of 20.
_JOB_RUNS = 3
#: How long a worker in a process of its own drains once told to stop.
_DOOMED_DRAIN = 1.0
#: How far into a worker's 5 s pull its task is put: past the 4 s ack wait, so a lease counted from the pull's sending
#: would run out before the task's first heartbeat.
_LATE_IN_PULL = 4.0
#: Long enough for a pull a stopped replica left open to expire (`maintenance.services.queue` pulls for 5 s).
_PULL_EXPIRY = 6.0
#: A credential with the worker's grants that runs out a minute after it is issued (nsc's least expiry). From then on the
#: bus takes it no longer, as it takes a revoked one no longer: it ends a session holding it, and refuses it at a connect.
_EXPIRING = "maintenance-worker-expiring"
_EXPIRY = 60.0
#: A worker in a process of its own, its pass bound the third argument: it says which task it runs, then holds it until
#: it is killed, its bound ends the process, `finish` on its stdin ends the pass, or `stop` there drains the worker.
_DOOMED = f"""
import asyncio, sys
from pathlib import Path
from maintenance.services.queue import TaskQueue, Worker

async def main(url: str, creds: str, max_pass: float) -> None:
    queue = await TaskQueue.connect(url, Path(creds).read_text(), name="doomed")

    finish = asyncio.Event()

    async def run(task) -> None:
        print("running", task.uuid, flush=True)
        await finish.wait()
        finish.clear()

    worker = Worker(
        queue, run=run, ready=lambda: True, heartbeat_seconds={_HEARTBEAT}, ack_wait_seconds={_ACK_WAIT}, max_deliver={_MAX_DELIVER},
        max_pass_seconds=max_pass, retry_ended_seconds=3600,
    )
    loop = asyncio.get_running_loop()
    stop = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stop), sys.stdin)
    pulling = asyncio.create_task(worker.run_tasks())
    while (line := (await stop.readline()).strip()) == b"finish":
        finish.set()
    await worker.drain([pulling], {_DOOMED_DRAIN})

asyncio.run(main(sys.argv[1], sys.argv[2], float(sys.argv[3])))
"""
#: A process of its own that connects to the bus as a worker does, says so, and holds the connection.
_HOLDING = """
import asyncio, sys
from pathlib import Path
from maintenance.services.queue import TaskQueue

async def main(url: str, creds: str) -> None:
    await TaskQueue.connect(url, Path(creds).read_text(), name="holding")
    print("connected", flush=True)
    await asyncio.sleep(3600)

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""


class _Runs:
    """Which worker ran which table's task, in order. A table in `held` holds its run until the test sets its event; a
    table in `at_start` has what its probe answers when the run starts kept in `seen`."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, str]] = []
        self.held: dict[str, asyncio.Event] = {}
        self.at_start: dict[str, Callable[[], object]] = {}
        self.seen: dict[str, object] = {}

    def of(self, worker: str) -> Callable[[Task], Awaitable[None]]:
        async def run(task: Task) -> None:
            if (probe := self.at_start.get(task.uuid)) is not None:
                self.seen[task.uuid] = probe()
            self.ran.append((worker, task.uuid))
            gate = self.held.get(task.uuid)
            await (gate.wait() if gate is not None else asyncio.sleep(1.0))

        return run

    def times(self, uuid: str) -> int:
        return sum(1 for _, ran in self.ran if ran == uuid)


class _Ended(Exception):  # noqa: N818 — a replica's end, raised where a worker ends its process
    """Raised where an in-process replica's worker would end its process (`maintenance.services.queue.end_replica`)."""


class _Replica:
    """One worker as a replica runs it: a connection of its own as `maintenance-worker`, both of its loops, and the
    consumer's lease. Where the worker would end its process, the replica stops its loops instead and keeps the reason
    in `ended`, so the run fails on a named claim rather than losing the test runner."""

    def __init__(self, queue: TaskQueue, name: str, runs: _Runs, *, ready: Callable[[], bool]) -> None:
        self.name = name
        self.ended: list[str] = []
        self._queue = queue
        self._worker = Worker(
            queue,
            run=runs.of(name),
            ready=ready,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=_MAX_DELIVER,
            max_pass_seconds=600,
            retry_ended_seconds=3600,
            end_process=self._end_process,
        )
        self._loops = [asyncio.create_task(self._worker.run_tasks()), asyncio.create_task(self._worker.run_due())]

    @classmethod
    async def start(cls, url: str, credential: Credential, name: str, runs: _Runs, *, ready: Callable[[], bool]) -> _Replica:
        return cls(await TaskQueue.connect(url, credential.creds_file.read_text(), name=name), name, runs, ready=ready)

    def _end_process(self, event: str, **extra: object) -> NoReturn:
        self.ended.append(event)
        for loop in self._loops:
            loop.cancel()
        raise _Ended(event)

    async def stop(self) -> None:
        self._worker.stop()
        for loop in self._loops:
            loop.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        await self._queue.close()


async def _doomed(url: str, credential: Credential, max_pass_seconds: float) -> asyncio.subprocess.Process:
    """A worker in a process of its own, its pass bound ``max_pass_seconds``."""
    return await asyncio.create_subprocess_exec(
        *(sys.executable, "-c", _DOOMED, url, str(credential.creds_file), str(max_pass_seconds)),
        cwd=REPO,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )


async def _holding(url: str, credential: Credential, log: Path) -> asyncio.subprocess.Process:
    """A connection to the bus as a worker makes it, held in a process of its own that writes what it logs to ``log``."""
    with log.open("wb") as logged:
        return await asyncio.create_subprocess_exec(
            *(sys.executable, "-c", _HOLDING, url, str(credential.creds_file)), cwd=REPO, stdout=asyncio.subprocess.PIPE, stderr=logged
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
    """The chart's stream Job, run as `admin` against this broker, as the estate runs it before maintenance starts: a run
    that fails is run again, as Kubernetes runs a failed Job's pod again (the chart's `backoffLimit`). Creating a stream
    copied to three servers can outlast the CLI's request deadline on a loaded host (measured 2026-11-09), and the Job is
    idempotent, so a second run finishes what the first began."""
    job = next(d for d in docs if d.get("kind") == "Job" and "-nats-stream-" in d["metadata"]["name"])
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    rendered = re.search(r'S="(nats://[^"]+)"', script)
    assert rendered, "the stream Job's script names no broker"
    runs = [run_stream_job(tools.job_cli, script.replace(rendered.group(1), broker.url), admin, home, 300.0)]
    while runs[-1].returncode and len(runs) < _JOB_RUNS:
        runs.append(run_stream_job(tools.job_cli, script.replace(rendered.group(1), broker.url), admin, home, 300.0))
    done = runs[-1]
    assert done.returncode == 0, f"the stream Job exited {done.returncode} on each of {len(runs)} runs: {(done.stdout + done.stderr)[-800:]}"


def _the_bus(tmp_path: Path, extra: dict[str, Permissions] | None = None) -> tuple[Tools, tuple[dict[str, Any], ...], Mint, Broker]:
    """The bus as the chart declares it, at the lane's short timings: `admin`, the worker's user and the store's minted with
    the grants the render issues (and ``extra`` users the test needs for itself), and the three-node cluster started."""
    tools = Tools.from_env(os.environ)
    values = Path(os.environ.get(VALUES_ENV) or REPO / "chart" / "values.yaml")
    queue_values = (
        *("--set", f"maintenance.queue.ackWaitSeconds={_ACK_WAIT}", "--set", f"maintenance.queue.heartbeatSeconds={_HEARTBEAT}"),
        *("--set", f"maintenance.queue.maxDeliver={_MAX_DELIVER}"),
    )
    docs = render(*DEFAULT_ARGS, "-f", str(values), *queue_values)
    issued = nats_users(docs)
    rendered = {"admin", _WORKER, _STORE}
    table = {user: Permissions(publish=tuple(sorted(issued[user][0])), subscribe=tuple(sorted(issued[user][1]))) for user in rendered if user in issued}
    assert set(table) == rendered, f"the render with {values} issues no {sorted(rendered - set(table))}"
    mint_ = mint(tools.nsc, {**table, **(extra or {})}, tmp_path / "nsc")
    broker = Broker(tools.nats_server, mint_, tmp_path / "broker")
    broker.start()
    return tools, docs, mint_, broker


@pytest.mark.asyncio
async def test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other(tmp_path: Path) -> None:
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    runs = _Runs()
    replicas: list[_Replica] = []
    doomed: list[asyncio.subprocess.Process] = []
    lane: TaskQueue | None = None
    sniffer: Client | None = None
    started: list[_Replica] = []

    async def start(name: str, ready: Callable[[], bool]) -> _Replica:
        replica = await _Replica.start(broker.url, worker, name, runs, ready=ready)
        replicas.append(replica)
        started.append(replica)
        return replica

    # Every pull a worker makes for a task, its reply inbox, as the rig sees it go by.
    pulls: list[str] = []
    next_task = f"$JS.API.CONSUMER.MSG.NEXT.{TASKS_STREAM}.{TASK_CONSUMER}"

    async def pulled(msg: Msg) -> None:
        pulls.append(msg.reply)

    def refusals() -> list[Violation]:
        """What the broker refused the worker's user: a call the chart's table does not grant."""
        return [v for v in violations(broker.log_text()) if mint_.user_of(v.user_key) == _WORKER]

    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        # The commit lane's connection: the service's own publishes, under the same user.
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")
        sniffer = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        await sniffer.nc.subscribe(next_task, cb=pulled)
        await sniffer.settle()

        killer = Task(kind="table", uuid="killer", location="s3://bucket/killer")
        assert await lane.enqueue(killer) is True, refusals()
        for _ in range(_MAX_DELIVER):
            doomed.append(hung := await _doomed(broker.url, worker, 1))
            assert await _said(hung) == ["running", "killer"], f"a worker did not take the task its table held; refused: {refusals()}"
            assert await asyncio.wait_for(hung.wait(), 30) == _HUNG_EXIT, "a pass past twice its bound did not end its worker's process"

        dying = Task(kind="table", uuid="dying", location="s3://bucket/dying")
        doomed.append(dier := await _doomed(broker.url, worker, 600))

        async def killer_ended() -> bool:
            return "killer" not in await lane.held("task", "table")

        # The doomed worker first takes "killer"'s last delivery and ends it unrun; the late put is timed from a later pull.
        assert await _until(killer_ended, _ACK_WAIT + 15), "a task that outlived its worker on every delivery still holds its table"
        assert runs.times("killer") == 0, "a task past its last delivery was run, not ended"
        assert "killer" in await lane.held("schedule", "table"), "an ended task's table holds no next try, so the hourly audit hands it back"
        seen = len(pulls)
        assert await _until(lambda: len(pulls) > seen, 15), "the doomed worker never pulled again"
        # Late in the pull, as a task put on an idle queue arrives: nothing to poll for.
        await asyncio.sleep(_LATE_IN_PULL)
        assert await lane.enqueue(dying) is True, refusals()
        said = await _said(dier)
        assert said == ["running", "dying"], f"the doomed worker said {said}; refused: {refusals()}"
        second_ready = asyncio.Event()
        second_ready.set()
        await start("second", second_ready.is_set)
        assert await lane.enqueue(dying) is False, "the queue took a second task for a table whose task is in flight"
        # Three ack waits in which the task must stay with the worker that still beats for it: nothing to poll for.
        await asyncio.sleep(3 * _ACK_WAIT)
        assert runs.times("dying") == 0, "a live worker's task went to another: its heartbeat did not hold it"
        dier.kill()
        await dier.wait()
        assert await _until(lambda: runs.times("dying") == 1, _ACK_WAIT + 15), f"a dead worker's task never reached the other; refused: {refusals()}"

        # The other worker takes nothing until the stranded one holds its task, once its own pull still open expires.
        second_ready.clear()
        await asyncio.sleep(_PULL_EXPIRY)
        link = Link(broker.port)
        await link.start()
        cut = Task(kind="table", uuid="cut", location="s3://bucket/cut")
        doomed.append(stranded := await _doomed(link.url, worker, 600))
        assert await lane.enqueue(cut) is True, refusals()
        said = await _said(stranded)
        assert said == ["running", "cut"], f"the stranded worker said {said}; refused: {refusals()}"
        second_ready.set()
        runs.at_start["cut"] = lambda: stranded.returncode
        outage = asyncio.create_task(link.cut(4 * _ACK_WAIT))
        ended_itself = await _until(lambda: stranded.returncode is not None, 2 * _ACK_WAIT)
        assert ended_itself and stranded.returncode == _HUNG_EXIT, f"a worker the bus stopped answering kept running its pass (exit {stranded.returncode})"
        assert await _until(lambda: runs.times("cut") == 1, _ACK_WAIT + 15), f"a stranded worker's task never reached the other; refused: {refusals()}"
        assert runs.seen["cut"] == _HUNG_EXIT, "the task went to another worker while the one the bus stopped answering still ran it"
        await outage
        await link.stop()

        second_ready.clear()
        await asyncio.sleep(_PULL_EXPIRY)
        draining = Task(kind="table", uuid="drained", location="s3://bucket/drained")
        doomed.append(stopping := await _doomed(broker.url, worker, 600))
        assert await lane.enqueue(draining) is True, refusals()
        said = await _said(stopping)
        assert said == ["running", "drained"], f"the stopping worker said {said}; refused: {refusals()}"
        second_ready.set()
        runs.at_start["drained"] = lambda: stopping.returncode
        assert stopping.stdin is not None
        stopping.stdin.write(b"stop\n")
        await stopping.stdin.drain()
        ended_itself = await _until(lambda: stopping.returncode is not None, _DOOMED_DRAIN + 5)
        assert ended_itself and stopping.returncode == _HUNG_EXIT, f"a worker whose drain ended mid-pass kept its pass (exit {stopping.returncode})"
        assert await _until(lambda: runs.times("drained") == 1, _ACK_WAIT + 15), f"a drained worker's task never reached the other; refused: {refusals()}"
        assert runs.seen["drained"] == _HUNG_EXIT, "the task went to another worker while the one draining still ran it"

        second_ready.clear()
        await asyncio.sleep(_PULL_EXPIRY)
        link = Link(broker.port)
        await link.start()
        settling = Task(kind="table", uuid="settled", location="s3://bucket/settled")
        doomed.append(settler := await _doomed(link.url, worker, 600))
        assert await lane.enqueue(settling) is True, refusals()
        said = await _said(settler)
        assert said == ["running", "settled"], f"the settling worker said {said}; refused: {refusals()}"
        release_settled = runs.held["settled"] = asyncio.Event()
        second_ready.set()
        outage = asyncio.create_task(link.cut(4 * _ACK_WAIT))
        # The pass ends once the cut stands, so its ack cannot be answered: nothing to poll for.
        await asyncio.sleep(0.5)
        assert settler.stdin is not None
        settler.stdin.write(b"finish\n")
        await settler.stdin.drain()
        ended_itself = await _until(lambda: settler.returncode is not None, 2 * _ACK_WAIT)
        assert ended_itself and settler.returncode == _HUNG_EXIT, (
            f"a worker whose pass ended in a bus cut kept its ack for after it (exit {settler.returncode})"
        )
        assert await _until(lambda: runs.times("settled") == 1, _ACK_WAIT + 15), f"the settling worker's task never reached the other; refused: {refusals()}"
        await outage
        await link.stop()
        # The bus is back, and no ack held through the cut freed the task the other worker still runs.
        assert await lane.enqueue(settling) is False, "an ack sent once the bus came back freed a task another worker still ran"
        release_settled.set()

        await start("first", lambda: True)
        await start("not-ready", lambda: False)
        burst = [Task(kind="table", uuid=f"burst-{n}", location=f"s3://bucket/burst-{n}") for n in range(6)]
        due = datetime.now(UTC) + timedelta(seconds=2)
        held = [sorted(await asyncio.gather(*(lane.follow_up(task, due) for _ in range(3)), return_exceptions=True), key=repr) for task in burst]
        assert held == [[False, False, True]] * len(burst), f"a burst held other than one follow-up per table: {held}"
        assert await _until(lambda: all(runs.times(task.uuid) for task in burst), 30), f"the burst's passes did not all run: {runs.ran}; refused: {refusals()}"
        # An ack wait past the last pass, in which no pass may run a second time: nothing to poll for.
        await asyncio.sleep(_ACK_WAIT + 1)
        passes = [(name, uuid) for name, uuid in runs.ran if uuid.startswith("burst-")]
        assert Counter(uuid for _, uuid in passes) == Counter(task.uuid for task in burst), f"a burst's pass ran other than once: {passes}"
        assert {name for name, _ in passes} == {"first", "second"}, f"the two ready workers did not share the burst, or one not ready took a pass: {passes}"

        busy = Task(kind="table", uuid="busy", location="s3://bucket/busy")
        release = runs.held["busy"] = asyncio.Event()
        put = sorted(await asyncio.gather(*(lane.enqueue(busy) for _ in range(3)), return_exceptions=True), key=repr)
        assert put == [False, False, True], f"three puts of one table's task at once queued other than one: {put}"
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

        for replica in replicas:
            await replica.stop()
        replicas.clear()
        # The stopped replicas' pulls expire, so the next task can only answer a pull this part of the test made: nothing to poll for.
        await asyncio.sleep(_PULL_EXPIRY)
        beta_ready = asyncio.Event()
        await start("alpha", lambda: True)
        await start("beta", beta_ready.is_set)
        release_first = runs.held["first"] = asyncio.Event()
        release_late = runs.held["late"] = asyncio.Event()
        assert await lane.enqueue(Task(kind="table", uuid="first", location="s3://bucket/first")) is True
        assert await _until(lambda: runs.times("first") == 1, 15), f"the first task never ran; refused: {refusals()}"
        # Alpha's pull its running pass answered, replayed as a reconnect replays a pull it held back; the next task answers it.
        await sniffer.nc.publish(next_task, json.dumps({"batch": 1, "expires": 30_000_000_000}).encode(), reply=pulls[-1])
        await sniffer.settle()
        assert await lane.enqueue(Task(kind="table", uuid="late", location="s3://bucket/late")) is True
        beta_ready.set()
        assert await _until(lambda: runs.times("late") == 1, _ACK_WAIT + 15), (
            f"a task delivered to a pull its worker gave up on was never handed on: {runs.ran}"
        )
        release_first.set()
        # Alpha pulls again once its pass ends, and must not run the task its old pull was answered with: nothing to poll for.
        await asyncio.sleep(_ACK_WAIT)
        late = [name for name, uuid in runs.ran if uuid == "late"]
        assert late == ["beta"], f"a task ran off a delivery to a pull its worker had given up on, beside its redelivery: {late}"
        release_late.set()

        # A claim, and its renewal, asked again after an answer was lost find their own token standing, and no other's.
        mine = await lane.claim("table.asked-again", "mine", within=10)
        assert mine is not None, "the commit lane's connection could not claim a free table"
        renewed = await lane.renew("table.asked-again", "mine", mine, within=10)
        assert renewed is not None, "a claim's holder could not renew it"
        assert await lane.renew("table.asked-again", "mine", mine, within=10) == renewed, "a renewal asked again after its answer was lost was refused"
        assert await lane.claim("table.asked-again", "mine", within=10) == renewed, "a claim asked again after its answer was lost was refused"
        assert await lane.renew("table.asked-again", "theirs", mine, within=10) is None, "a stale renewal took a claim another holds"
        assert await lane.claim("table.asked-again", "theirs", within=10) is None, "a claim was taken while another's stood"
        await lane.release("table.asked-again", renewed)

        deliveries: list[Msg] = []

        async def delivered(msg: Msg) -> None:
            if msg.subject.endswith(".fenced"):
                deliveries.append(msg)

        await sniffer.nc.subscribe(f"{INBOX_PREFIX}.>", cb=delivered)
        await sniffer.settle()
        release_fenced = runs.held["fenced"] = asyncio.Event()
        fenced = Task(kind="table", uuid="fenced", location="s3://bucket/fenced")
        assert await lane.enqueue(fenced) is True
        assert await _until(lambda: runs.times("fenced") == 1 and bool(deliveries), 15), f"the fenced task never ran; refused: {refusals()}"
        # A late ack on the delivery the live pass holds, as one the network held past a stale worker's lease lands.
        await sniffer.nc.publish(deliveries[0].reply, b"+ACK")
        await sniffer.settle()

        async def freed() -> bool:
            return "fenced" not in await lane.held("task", "table")

        assert await _until(freed, 10), "a late ack did not free the task a live pass holds"
        assert await lane.enqueue(fenced) is True, "the queue refused a task for a table whose task a late ack freed"
        # The next task's delivery comes at once, and must wait out the live pass's claim: nothing to poll for.
        await asyncio.sleep(2 * _ACK_WAIT)
        assert runs.times("fenced") == 1, f"a pass ran beside another of its table once a late ack freed its task: {runs.ran}"
        release_fenced.set()
        assert await _until(lambda: runs.times("fenced") == 2, _ACK_WAIT + 30), f"the table's next task never ran once its pass ended: {runs.ran}"
        ended = {replica.name: replica.ended for replica in started if replica.ended}
        assert not ended, f"an in-process replica ended where a worker ends its process: {ended}"
    finally:
        # The cluster stops even when a client's close raises (a drain that times out does): its servers would outlive the run.
        try:
            for process in doomed:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            for replica in replicas:
                await replica.stop()
            if lane is not None:
                await lane.close()
            if sniffer is not None:
                await sniffer.close()
        finally:
            broker.stop()
    assert refusals() == [], "the broker refused the worker a call the chart must grant"


@pytest.mark.asyncio
async def test_a_replica_runs_a_failed_loop_again_and_puts_back_schedules_the_bus_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    ran: list[str] = []
    failures: list[str] = []
    run_due = Worker.run_due

    async def record(task: Task, **_: object) -> None:
        ran.append(task.uuid)

    async def fails_once(self: Worker) -> None:
        if not failures:
            failures.append("run_due")
            raise RuntimeError("a fault no loop handles")
        await run_due(self)

    monkeypatch.setattr(tasks, "run_task", record)
    monkeypatch.setattr(tasks, "_CADENCE_SECONDS", 2.0)
    monkeypatch.setattr(Worker, "run_due", fails_once)
    # The service reads its secrets from the store as the pod does, presenting its ServiceAccount token.
    (token := tmp_path / "token").write_text("a projected token")
    monkeypatch.setenv("MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("MAINTENANCE_S3_STS_ENDPOINT", "http://store.example:9000")
    settings = MaintenanceSettings.model_validate(
        {
            **{"s3_bucket": "b", "nats_url": broker.url, "secrets_from_dapr": True, "drain_seconds": 5},
            **{"task_heartbeat_seconds": _HEARTBEAT, "task_ack_wait_seconds": _ACK_WAIT, "task_max_deliver": _MAX_DELIVER},
        }
    )
    # The session the service's lifespan exchanges its token for (`lakehouse.store_session.start_store_session`), which its
    # store-events reader signs with; this test reads no object.
    sessions = store_session.SessionCache()
    monkeypatch.setattr(store_session, "SESSIONS", sessions)
    identity = settings.web_identity
    assert identity is not None, "the service holds no web identity, so it would sign with no session"
    session = S3KeyPair(access_key_id="TESTSESSION", secret_access_key=SecretStr("test-only"), session_token=SecretStr("test-only"))
    sessions.hold(identity, session, asked_at=sessions.now(), lifetime_seconds=3600)
    cadences = [Task(kind="audit").subject("schedule"), Task(kind="scan").subject("schedule")]
    rig: Client | None = None
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        rig = reader = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)

        async def held() -> list[bool]:
            answers = [await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": subject}) or {} for subject in cadences]
            return ["message" in answer for answer in answers]

        async with AsyncExitStack() as stack:
            with respx.mock(assert_all_called=True) as store:
                secret = f"http://localhost:3500/v1.0/secrets/{settings.dapr_secret_store}/{settings.nats_credentials_secret}"
                store.get(secret).mock(return_value=httpx.Response(200, json={"jwt": worker.jwt, "seed": worker.seed()}))
                lane = await stack.enter_async_context(tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()))
            assert lane is not None, "the replica started with no bus"
            assert await held() == [True, True], "a replica started without the audit's and the scan's schedules"

            healed = Task(kind="table", uuid="healed", location="s3://bucket/healed")
            assert await lane.follow_up(healed, datetime.now(UTC) + timedelta(seconds=1)) is True
            assert await _until(lambda: "healed" in ran, 20), f"a follow-up never ran once the loop moving it had failed (failures: {failures})"

            for subject in cadences:
                await reader.request(f"$JS.API.STREAM.PURGE.{SCHEDULES_STREAM}", {"filter": subject})

            async def put_back() -> bool:
                return await held() == [True, True]

            assert await _until(put_back, 10), "the schedules the bus lost were never put back"
    finally:
        try:
            if rig is not None:
                await rig.close()
        finally:
            broker.stop()
    assert failures == ["run_due"], "the loop moving fired schedules never failed, so its running again proves nothing"


@pytest.mark.asyncio
async def test_a_worker_whose_credential_the_bus_takes_no_longer_ends_itself(tmp_path: Path) -> None:
    """nats-py keeps the credential a worker read at start: it closes for good on a session the server ends, and retries a
    credential the server refuses for ever. So a worker the bus takes its credential from no longer ends its process, and
    its pod, started again, reads the credential the store then holds. One connection is held while its credential runs
    out, which the server ends; another is cut through a `Link` until the credential has run out, and refused when it
    reconnects."""
    tools, docs, _, broker = _the_bus(tmp_path)
    publish, subscribe = nats_users(docs)[_WORKER]
    grants = Permissions(publish=tuple(sorted(publish)), subscribe=tuple(sorted(subscribe)), expiry="1m")
    link = Link(broker.port)
    await link.start()
    holding: list[asyncio.subprocess.Process] = []
    logs = (tmp_path / "held.log", tmp_path / "cut.log")
    try:
        credential = issue(tools.nsc, tmp_path / "nsc", _EXPIRING, grants)
        holding += [await _holding(broker.url, credential, logs[0]), await _holding(link.url, credential, logs[1])]
        held, cut = holding
        assert [await _said(held), await _said(cut)] == [["connected"], ["connected"]], "a worker could not connect with its own credential"

        outage = asyncio.create_task(link.cut(_EXPIRY + 15))
        session_ended = await _until(lambda: held.returncode is not None, _EXPIRY + 30)
        assert session_ended and held.returncode == _HUNG_EXIT, (
            f"a worker whose session the bus ended kept its process (exit {held.returncode}): {logs[0].read_text()[-600:]}"
        )
        await outage
        refused = await _until(lambda: cut.returncode is not None, 30)
        assert refused and cut.returncode == _HUNG_EXIT, (
            f"a worker the bus refused at a reconnect kept reconnecting (exit {cut.returncode}): {logs[1].read_text()[-600:]}"
        )
    finally:
        for process in holding:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await link.stop()
        broker.stop()


def _schedule_of(answer: dict[str, Any] | None) -> str | None:
    """The `Nats-Schedule` header of a message a `STREAM.MSG.GET` answered with, or None when it found none."""
    stored = (answer or {}).get("message")
    if not stored or not stored.get("hdrs"):
        return None
    for line in base64.b64decode(stored["hdrs"]).decode().split("\r\n"):
        name, _, value = line.partition(":")
        if name == "Nats-Schedule":
            return value.strip()
    return None


async def _is_held(read: Callable[[], Awaitable[datetime | None]]) -> bool:
    return await read() is not None


def _held_at(answer: dict[str, Any] | None) -> datetime | None:
    """When the `@at` schedule a `STREAM.MSG.GET` answered with fires, or None when it found none."""
    schedule = _schedule_of(answer)
    return datetime.strptime(schedule.removeprefix("@at "), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if schedule else None


def _write_event(key: str, *, bucket: object = "b") -> bytes:
    """A store's event of one object write, in S3's record shape; a ``bucket`` that is no name is sent as it is."""
    named = {"name": bucket} if isinstance(bucket, str) else bucket
    return json.dumps({"Records": [{"eventName": "s3:ObjectCreated:Put", "s3": {"bucket": named, "object": {"key": key}}}]}).encode()


async def _never_run(task: Task) -> None:
    raise AssertionError(f"a store-event worker ran the task {task}")


@pytest.mark.asyncio
async def test_a_worker_settles_each_store_event_by_what_its_handling_found(tmp_path: Path) -> None:
    """A store event is acknowledged once its table's pass is held, ended at once when it is no store event, delivered
    again after a delay when the store refused a claim's read, the delay doubling with each delivery, and ended on its last
    delivery when the store refuses it on every one. The handling is the service's own
    (`maintenance.services.store_events.StoreEvents`), on a control root of the test's own, where a claim the store cannot
    read is a directory standing at its key. The worker ends an event on the third delivery, which the consumer the
    render declares at `_MAX_DELIVER` allows (one past it)."""
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    control = tmp_path / "control"
    control.mkdir()
    settings = MaintenanceSettings(s3_bucket="b", control_root=str(control), commit_pass_delay_seconds=3600)
    claims = location_claims.ClaimStore(control_root=str(control))
    for table in ("held", "flaky", "locked"):
        location_claims.take(claims, f"s3://b/{table}", f"db${table}", ["db", table], is_gone=lambda _claim: False, uuid=table)
    unreadable_claims = {table: control / location_claims.claim_key(f"s3://b/{table}") for table in ("flaky", "locked")}
    for claim in unreadable_claims.values():
        claim.unlink()
        claim.mkdir()
    events = {
        "held": _write_event("held/data/a.lance"),
        "unreadable": _write_event("held/data/b.lance", bucket=["b"]),
        "flaky": _write_event("flaky/data/c.lance"),
        "locked": _write_event("locked/data/d.lance"),
    }
    handled: dict[bytes, list[float]] = {body: [] for body in events.values()}
    queue: TaskQueue | None = None
    store: Client | None = None
    rig: Client | None = None
    loops: list[asyncio.Task[None]] = []
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        queue = await TaskQueue.connect(broker.url, mint_.credentials[_WORKER].creds_file.read_text(), name="store-events")
        replica = StoreEvents(settings, queue)
        loop = asyncio.get_running_loop()

        async def handle(body: bytes) -> int:
            handled[body].append(loop.time())
            return await replica.hold_passes(body)

        worker = Worker(
            queue,
            run=_never_run,
            ready=lambda: True,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=_MAX_DELIVER + 1,
            max_pass_seconds=600,
            retry_ended_seconds=3600,
            store_events=handle,
        )
        loops.append(asyncio.create_task(worker.run_store_events()))
        store = await Client.connect(broker.url, mint_.credentials[_STORE], _STORE)
        rig = reader = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        for body in events.values():
            await store.nc.publish("store.events", body)
        await store.settle()
        assert await _until(lambda: len(handled[events["flaky"]]) == 1, 30), f"the event the store refused was never handled: {handled}"
        unreadable_claims["flaky"].rmdir()
        location_claims.take(claims, "s3://b/flaky", "db$flaky", ["db", "flaky"], is_gone=lambda _claim: False, uuid="flaky")

        async def settled() -> bool:
            state = (await reader.request(f"$JS.API.STREAM.INFO.{STORE_EVENTS_STREAM}") or {}).get("state", {})
            return len(handled[events["flaky"]]) == 2 and len(handled[events["locked"]]) == 3 and state.get("messages") == 0

        assert await _until(settled, 90), f"the store's events were not all settled: {handled}"

        async def follow_up(table: str) -> dict[str, Any] | None:
            subject = Task(kind="table", uuid=table).follow_up_subject("schedule")
            return await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": subject})

        held = {table: _held_at(await follow_up(table)) is not None for table in ("held", "flaky", "locked")}
    finally:
        try:
            for running in loops:
                running.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            for client in (store, rig):
                if client is not None:
                    await client.close()
            if queue is not None:
                await queue.close()
        finally:
            broker.stop()

    locked = handled[events["locked"]]
    waits = [round(later - earlier) for earlier, later in pairwise(locked)]
    assert {name: len(handled[body]) for name, body in events.items()} == {"held": 1, "unreadable": 1, "flaky": 2, "locked": 3}
    assert 10 <= waits[0] < 25 and waits[1] >= 25, f"the refused event came back after {waits} s, not after a doubling retry delay"
    assert held == {"held": True, "flaky": True, "locked": False}


@pytest.mark.asyncio
async def test_a_task_whose_run_fails_comes_back_later_and_ends_on_its_last_delivery_and_an_unreadable_one_ends_at_once(tmp_path: Path) -> None:
    """A task whose run raises is delivered again after its retry delay, and on its last delivery is ended, its next try
    held a recheck out; a message on the queue that is no task is ended without running."""
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    runs: list[float] = []
    queue: TaskQueue | None = None
    writer: Client | None = None
    rig: Client | None = None
    loops: list[asyncio.Task[None]] = []
    ended_at: datetime | None = None
    held_at: datetime | None = None
    failing = Task(kind="table", uuid="failing", location="s3://bucket/failing")
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        queue = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="failing")
        loop = asyncio.get_running_loop()

        async def fails(task: Task) -> None:
            runs.append(loop.time())
            raise RuntimeError(f"the store refused the pass of {task.uuid}")

        def ends_its_process(event: str, **extra: object) -> NoReturn:
            raise AssertionError(f"the worker ended its process: {event} {extra}")

        replica = Worker(
            queue,
            run=fails,
            ready=lambda: True,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=_MAX_DELIVER,
            max_pass_seconds=600,
            retry_ended_seconds=3600,
            end_process=ends_its_process,
        )
        loops.append(asyncio.create_task(replica.run_tasks()))
        writer = await Client.connect(broker.url, worker, "garbage", inbox=INBOX_PREFIX)
        await writer.nc.publish("maintenance.task.table.garbage", b"no task")
        await writer.settle()
        assert await queue.enqueue(failing) is True
        rig = reader = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)

        async def ended() -> bool:
            return len(runs) == _MAX_DELIVER and not await queue.held("task", "table")

        assert await _until(ended, 60), f"the failing task was not ended on its last delivery: {len(runs)} runs"
        ended_at = datetime.now(UTC)

        async def next_try() -> datetime | None:
            return _held_at(await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": failing.subject("schedule")}))

        assert await _until(lambda: _is_held(next_try), 10), "the ended task's next try was never held"
        held_at = await next_try()
    finally:
        try:
            for running in loops:
                running.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            for client in (writer, rig):
                if client is not None:
                    await client.close()
            if queue is not None:
                await queue.close()
        finally:
            broker.stop()

    assert 10.0 <= runs[1] - runs[0] < 30.0, f"the failed task came back after {runs[1] - runs[0]:.1f} s, not after its retry delay"
    assert held_at is not None and abs((held_at - ended_at).total_seconds() - 3600) < 10, f"the ended task's next try is held for {held_at}, not a recheck out"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "taken_by"),
    [
        pytest.param(_MAX_DELIVER, "next", id="with-a-delivery-to-spare-it-goes-to-another-replica"),
        pytest.param(1, "stopping", id="on-its-last-delivery-it-is-handled-where-it-arrived"),
    ],
)
async def test_a_task_a_pull_answers_as_its_worker_stops(tmp_path: Path, allowed: int, taken_by: str) -> None:
    """A pull in flight when a worker is asked to stop can still be answered. With a delivery to spare the task is given
    back at once and another replica runs it, rather than its pass starting under the drain; on the last delivery the
    worker allows it is handled where it arrived, since a hand-back would spend the consumer's last delivery and leave
    the task on the queue, undelivered, for good. And a connection the bus has gone from still closes."""
    tools, docs, mint_, broker = _the_bus(tmp_path)
    worker = mint_.credentials[_WORKER]
    ran: list[str] = []
    queues: list[TaskQueue] = []
    loops: list[asyncio.Task[None]] = []
    task = Task(kind="table", uuid="late", location="s3://bucket/late")
    closed_without_its_bus: BaseException | None = None

    def ends_its_process(event: str, **extra: object) -> NoReturn:
        raise AssertionError(f"the worker ended its process: {event} {extra}")

    def replica(queue: TaskQueue, name: str, max_deliver: int) -> Worker:
        async def run(_task: Task) -> None:
            ran.append(name)

        return Worker(
            queue,
            run=run,
            ready=lambda: True,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=max_deliver,
            max_pass_seconds=600,
            retry_ended_seconds=3600,
            end_process=ends_its_process,
        )

    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        for name in ("stopping", "next"):
            queues.append(await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name=name))
        stopping = replica(queues[0], "stopping", allowed)
        loops.append(asyncio.create_task(stopping.run_tasks()))
        await asyncio.sleep(1.0)  # its first pull is out, and waits up to five seconds for a task
        stopping.stop()
        assert await queues[1].enqueue(task) is True
        await asyncio.wait_for(loops[0], timeout=15)
        loops.append(asyncio.create_task(replica(queues[1], "next", _MAX_DELIVER).run_tasks()))

        async def settled() -> bool:
            return bool(ran) and not await queues[1].held("task", "table")

        assert await _until(settled, 30), f"the task was never run and settled: ran by {ran}"
    finally:
        for running in loops:
            running.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        broker.stop()
        for queue in queues:
            try:
                await asyncio.wait_for(queue.close(), timeout=30)
            except BaseException as exc:  # noqa: BLE001 — kept for the assertion below
                closed_without_its_bus = exc

    assert ran == [taken_by]
    assert closed_without_its_bus is None, f"a connection whose bus is gone did not close: {closed_without_its_bus!r}"


@pytest.mark.asyncio
async def test_an_idle_worker_stops_and_closes_its_connection_at_once(tmp_path: Path) -> None:
    """A worker with nothing to do stops within one pull's wait, and its connection then closes at once: each loop ends
    the subscription it pulled on, so the connection's drain waits on none (measured on the estate before each did:
    an idle worker took 34 s to stop, the client's 30 s drain timeout, on every rollout)."""
    tools, docs, mint_, broker = _the_bus(tmp_path)
    queue: TaskQueue | None = None
    took: float | None = None

    def ends_its_process(event: str, **extra: object) -> NoReturn:
        raise AssertionError(f"the worker ended its process: {event} {extra}")

    async def handled(_body: bytes) -> None:
        return None

    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        queue = await TaskQueue.connect(broker.url, mint_.credentials[_WORKER].creds_file.read_text(), name="idle")
        idle = Worker(
            queue,
            run=_never_run,
            ready=lambda: True,
            heartbeat_seconds=_HEARTBEAT,
            ack_wait_seconds=_ACK_WAIT,
            max_deliver=_MAX_DELIVER,
            max_pass_seconds=600,
            retry_ended_seconds=3600,
            end_process=ends_its_process,
            store_events=handled,
        )
        loops = [asyncio.create_task(loop()) for loop in (idle.run_tasks, idle.run_due, idle.run_store_events)]
        await asyncio.sleep(2.0)  # each loop's pull is out
        began = asyncio.get_running_loop().time()
        await idle.drain(loops, 20)
        await queue.close()
        took = asyncio.get_running_loop().time() - began
    finally:
        broker.stop()

    assert took is not None and took < 10, f"an idle worker took {took:.1f} s to stop and close"


@pytest.mark.asyncio
async def test_the_audit_and_the_scan_never_run_at_once(tmp_path: Path) -> None:
    """The audit and the scan each repair the estate from a drift report of their own, so one waits out the other's claim
    while it runs, on two replicas, and runs once it is done."""
    tools, docs, mint_, broker = _the_bus(tmp_path)
    worker = mint_.credentials[_WORKER]
    runs = _Runs()
    runs.held[""] = release = asyncio.Event()
    replicas: list[_Replica] = []
    lane: TaskQueue | None = None
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")
        for name in ("audit-replica", "scan-replica"):
            replicas.append(await _Replica.start(broker.url, worker, name, runs, ready=lambda: True))
        assert await lane.enqueue(Task(kind="audit")) is True
        assert await lane.enqueue(Task(kind="scan")) is True
        assert await _until(lambda: len(runs.ran) >= 1, 15), f"neither the audit nor the scan ran: {runs.ran}"
        # The other's first delivery waits one ack wait for the claim, and comes back after its retry delay: nothing to poll
        # for while it must not run, and released before its second delivery, which a held claim would give back again.
        await asyncio.sleep(3 * _ACK_WAIT)
        beside = list(runs.ran)
        release.set()
        assert await _until(lambda: len(runs.ran) == 2, 60), f"the other never ran once the first was done: {runs.ran}"
    finally:
        try:
            for replica in replicas:
                await replica.stop()
            if lane is not None:
                await lane.close()
        finally:
            broker.stop()
    assert len(beside) == 1, f"the audit and the scan ran at once: {beside}"


@pytest.mark.asyncio
async def test_an_audit_the_scan_outlasts_is_given_up_and_is_not_counted_as_a_task_that_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit that finds the scan's claim held on every delivery failed at nothing: it leaves the queue on its last
    delivery without running and without the count the ended-tasks alert reads, and its next firing brings it back."""
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    runs = _Runs()
    runs.held[""] = release = asyncio.Event()
    ended: list[str] = []
    monkeypatch.setattr(worker_queue, "record_task_ended", lambda *, kind: ended.append(kind))
    replicas: list[_Replica] = []
    lane: TaskQueue | None = None
    rig: Client | None = None
    left = False
    ran: list[tuple[str, str]] = []
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")
        rig = reader = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        for name in ("scan-replica", "audit-replica"):
            replicas.append(await _Replica.start(broker.url, worker, name, runs, ready=lambda: True))
        assert await lane.enqueue(Task(kind="scan")) is True
        assert await _until(lambda: len(runs.ran) == 1, 15), f"the scan never ran: {runs.ran}"
        assert await lane.enqueue(Task(kind="audit")) is True

        async def the_audit_left_the_queue() -> bool:
            answer = await reader.request(f"$JS.API.STREAM.MSG.GET.{TASKS_STREAM}", {"last_by_subj": Task(kind="audit").subject("task")})
            return answer is not None and "error" in answer

        left = await _until(the_audit_left_the_queue, 120)
        # Read before the scan is let go: a replica stopped mid-pass leaves its task to the other, which would run it.
        ran = list(runs.ran)
    finally:
        release.set()
        try:
            for replica in replicas:
                await replica.stop()
            for client in (lane, rig):
                if client is not None:
                    await client.close()
        finally:
            broker.stop()

    assert left, "the audit was still queued behind the scan's claim after every delivery it had"
    assert (ended, ran) == ([], [("scan-replica", "")]), f"the audit was counted as ended, or ran beside the scan: {ended} {ran}"


@pytest.mark.asyncio
async def test_a_pass_whose_claim_is_gone_ends_its_replica(tmp_path: Path) -> None:
    """A pass renews its claim on its table with each heartbeat, and one that finds the claim no longer its own cannot
    tell whether another pass has taken the table: the replica ends itself rather than run beside it."""
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    runs = _Runs()
    runs.held["claimed"] = asyncio.Event()
    replica: _Replica | None = None
    lane: TaskQueue | None = None
    rig: Client | None = None
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")
        rig = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        replica = await _Replica.start(broker.url, worker, "claiming", runs, ready=lambda: True)
        assert await lane.enqueue(Task(kind="table", uuid="claimed", location="s3://bucket/claimed")) is True
        assert await _until(lambda: runs.times("claimed") == 1, 15), f"the task never ran: {runs.ran}"

        purged = await rig.request("$JS.API.STREAM.PURGE.KV_MAINTENANCE_CLAIMS")

        ended = await _until(lambda: bool(replica is not None and replica.ended), 3 * _ACK_WAIT)
    finally:
        try:
            if replica is not None:
                await replica.stop()
            for client in (lane, rig):
                if client is not None:
                    await client.close()
        finally:
            broker.stop()

    assert purged is not None and purged.get("success") is True, f"the claims were not purged: {purged}"
    assert ended and replica is not None and replica.ended == ["maintenance_claim_lost"], (
        f"the replica ran on without its claim: {replica.ended if replica else None}"
    )


@pytest.mark.asyncio
async def test_a_fired_schedule_that_is_no_task_is_ended_and_not_delivered_again(tmp_path: Path) -> None:
    """What a schedule fires is moved onto the queue as the task it carries; one that carries none can never be moved, so
    it is ended, where giving it back would deliver it for ever (the consumer of fired schedules has no delivery bound)."""
    fires = Permissions(publish=(*_RIG_GRANTS.publish, "maintenance.due.>"), subscribe=_RIG_GRANTS.subscribe)
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: fires})
    replica: _Replica | None = None
    rig: Client | None = None
    ended = False
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        rig = reader = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        replica = await _Replica.start(broker.url, mint_.credentials[_WORKER], "mover", _Runs(), ready=lambda: True)
        await reader.nc.publish("maintenance.due.table.garbage", b"no task")
        await reader.settle()

        async def the_schedules_hold_nothing() -> bool:
            state = (await reader.request(f"$JS.API.STREAM.INFO.{SCHEDULES_STREAM}") or {}).get("state", {})
            return state.get("messages") == 0 and state.get("last_seq", 0) >= 1

        ended = await _until(the_schedules_hold_nothing, 20)
    finally:
        try:
            if replica is not None:
                await replica.stop()
            if rig is not None:
                await rig.close()
        finally:
            broker.stop()

    assert ended, "the fired schedule that is no task was still held, to be delivered again"


@pytest.mark.asyncio
async def test_a_task_whose_claim_the_bus_does_not_answer_is_held_until_it_is_and_then_runs(tmp_path: Path) -> None:
    """A claim the bus cannot answer, as with the claims bucket not yet declared, says nothing of whether another pass
    holds the table: the worker keeps the task and asks again, and runs it once the claim is answered, without spending
    a delivery on it."""
    tools, docs, mint_, broker = _the_bus(tmp_path, {_RIG: _RIG_GRANTS})
    worker = mint_.credentials[_WORKER]
    runs = _Runs()
    replica: _Replica | None = None
    lane: TaskQueue | None = None
    rig: Client | None = None
    ran_unclaimed: list[tuple[str, str]] = []
    ran = False
    redelivered: object = None
    try:
        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)
        lane = await TaskQueue.connect(broker.url, worker.creds_file.read_text(), name="commit-lane")
        rig = await Client.connect(broker.url, mint_.credentials[_RIG], _RIG, answer_within=10.0)
        dropped = await rig.request("$JS.API.STREAM.DELETE.KV_MAINTENANCE_CLAIMS")
        assert dropped is not None and dropped.get("success") is True, f"the claims bucket was not removed: {dropped}"
        replica = await _Replica.start(broker.url, worker, "waiting", runs, ready=lambda: True)
        assert await lane.enqueue(Task(kind="table", uuid="unclaimed", location="s3://bucket/unclaimed")) is True
        # Longer than the worker's wait between asks, and well inside the first redelivery delay (15 s).
        await asyncio.sleep(_ACK_WAIT)
        ran_unclaimed = list(runs.ran)

        await asyncio.to_thread(_declare_the_queue, tools, docs, broker, mint_.credentials["admin"], tmp_path)

        ran = await _until(lambda: runs.times("unclaimed") == 1, 30)
        consumer = await rig.request(f"$JS.API.CONSUMER.INFO.{TASKS_STREAM}.{TASK_CONSUMER}") or {}
        redelivered = consumer.get("num_redelivered")
    finally:
        try:
            if replica is not None:
                await replica.stop()
            for client in (lane, rig):
                if client is not None:
                    await client.close()
        finally:
            broker.stop()

    assert ran_unclaimed == [], f"the task ran with no claim on its table: {ran_unclaimed}"
    assert ran and replica is not None and replica.ended == [], (
        f"the task did not run once its claim was answered: {runs.ran} {replica.ended if replica else None}"
    )
    assert redelivered == 0, f"the unanswered claim spent {redelivered} of the task's deliveries"
