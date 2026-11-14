"""Maintenance's replicas share one work queue, and a dead replica's task goes to another ([[LH-494]] closes-when (b),
ADR 0198 §2).

The service's own worker (`maintenance.services.queue.Worker` over `TaskQueue`) on the bus the lane stands up: the NATS
fixture's three-node nats-server cluster in operator mode, the chart's stream Job run as `admin`, which declares
MAINTENANCE_TASKS, MAINTENANCE_SCHEDULES and their two durables at the chart's default replicas, a copy on every server as
on the estate, and every worker connected as `maintenance-worker` with the grants the dev seed issues, so a call the chart
does not grant fails here as it would on the estate. Only what a task runs is the
test's: it records which worker ran it, and holds while the test holds it. The render shortens the consumer's ack wait to
`_ACK_WAIT` seconds, so a dead worker's task moves within the test.

ONE BUS FOR THE MODULE, ITS QUEUE DECLARED AFRESH FOR EACH TEST. The cluster and every user any test connects as are made
once (`bus`); before each test the maintenance streams and the claims bucket are deleted and the stream Job declares them
again (`declared`), so no test reads a message, a delivery count or a claim another left, and a test that deletes a stream
leaves nothing behind. A broker refusal of the worker's user fails the test that drew it.

Each test claims one behaviour:

* A pass that runs past twice its bound ends its worker's process (exit 70); a task that outlived its worker on every
  `maintenance.queue.maxDeliver` delivery is ended unrun on the next, which frees its table and holds its next try a
  recheck out.
* A task put late in its worker's pull stays with that worker for as long as it beats, its lease counted from the task's
  arrival; the queue refuses a second task for a table whose task is in flight; a worker killed mid-run (SIGKILL) has its
  task go to another.
* A worker the bus stops answering mid-pass (its connection cut through a `Link`) ends its process (exit 70) before the
  ack wait its last answered heartbeat restarted runs out, though its records go to a Collector that never answers, so
  its task, handed to the other worker, starts only once the first has ended. A worker asked to stop whose drain ends
  with a pass still running does the same, and so does one whose pass ends while the bus is cut, so no ack held through
  the cut frees the task the other worker then runs.
* A task JetStream delivers to a pull its worker gave up on (one replayed, as a reconnect replays it, while the worker's
  pass runs) is never run off that delivery: it waits out its ack wait and runs once, on the worker that pulled it next.
* An ack the network held past its worker's lease lands on a delivery a live pass holds (the rig sends it, as a
  partition that heals would), freeing the table's task under that pass; a task put for the table then waits out the live
  pass's claim on it, and runs only once that pass is done.
* A burst of follow-ups put at once, as replicas taking a burst of store events put them, holds one per table; each held
  follow-up runs once; and two ready workers share the passes, each holding the first it takes so the other must take the
  next, while a worker that is not ready takes none. A copied stream refuses a put racing another for its subject with
  another code than a put after it (`nats_auth_rig.Broker`), and both mean the table is covered.
* Three puts of one table's task at once queue one. A follow-up that fires while that task holds its table waits until
  the task is done, then passes the table again: the commit it follows may be one the running pass read the table before.
* A claim and its renewal asked again after an answer was lost find their own token; a claim another holds is neither
  taken nor renewed; a release frees only its holder's claim.
* A worker whose claims bucket the bus holds no longer ends its task loop on the first claim it asks; a claim the bus does
  not answer holds the task until it is answered, spending no delivery.
* A task whose run fails, an error or a native library's panic, comes back after its retry delay, and on its last delivery
  is ended with its failure logged; an ended task's next try is held a recheck out; a message on the queue that is no task
  is ended without running.
* A store event is acknowledged once its table's pass is held, ended at once when it is no store event, delivered again
  after its delivery's retry delay when the store refused a claim's read, and ended on its last delivery when the store
  refuses it on every one.

A replica started as the service starts it (`maintenance.services.tasks.running_worker`, its bus credential read from a
stand-in of the store's secrets API) keeps its loops and its schedules: a loop that ends before the replica stops, with an
error or without, ends the replica's process; the audit's and the scan's schedules are asserted at its start, and put
back on the cadence the replica asserts them on once the bus loses them.

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
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
import pytest_asyncio
from measured_metrics import Measured
from nats.aio.msg import Msg
from nats_auth_rig import VALUES_ENV, Broker, Client, Credential, Link, Mint, Permissions, Tools, Violation, issue, mint, run_stream_job, violations
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, SecretStr

from lakehouse import location_claims, store_session
from lakehouse.control_emit import NoopControlEmitter
from lakehouse.governed import secrets
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.supervision import EndProcess, end_process
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import queue as worker_queue
from maintenance.services import tasks
from maintenance.services.queue import (
    CLAIMS_BUCKET,
    INBOX_PREFIX,
    SCHEDULES_STREAM,
    STORE_EVENTS_STREAM,
    TASK_CONSUMER,
    TASKS_STREAM,
    ClaimsBucketMissingError,
    Task,
    TaskKind,
    TaskQueue,
    Worker,
    sample_queue,
)
from maintenance.services.store_events import StoreEvents
from tests.secret_store_double import SecretStoreDouble
from tests.unit.chart_render import DEFAULT_ARGS, nats_users, render


pytestmark = pytest.mark.nats_auth

REPO = Path(__file__).resolve().parents[2]
_WORKER = "maintenance-worker"
#: The store's own bus user, which publishes each object write on `store.events` (`deploy/fixtures/rustfs`).
_STORE = "object-store"
#: The test's own user, every JetStream call granted: it reads and purges what the worker's schedules leave on the bus,
#: sees and replays the workers' pulls, and fires a schedule as the bus fires one.
_RIG = "rask-test-rig"
_RIG_GRANTS = Permissions(publish=("$JS.API.>", "$JS.ACK.>", "maintenance.due.>"), subscribe=("_INBOX.>", "$JS.API.CONSUMER.MSG.NEXT.>", f"{INBOX_PREFIX}.>"))
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
#: How long a worker's pull waits at the server for a task (`maintenance.services.queue`).
_FETCH_SECONDS = 5.0
#: How long the link holds each frame a worker sends on its way to the broker: the time a slow path to the stream's leader
#: adds, short of the heartbeat, within which a worker's claim and heartbeat must be answered.
_PULL_TRANSIT = 0.3
#: A credential with the worker's grants that runs out a minute after it is issued (nsc's least expiry). From then on the
#: bus takes it no longer, as it takes a revoked one no longer: it ends a session holding it, and refuses it at a connect.
_EXPIRING = "maintenance-worker-expiring"
_EXPIRY = 60.0
#: A worker in a process of its own, its pass bound the third argument: it says which task it runs, then holds it until
#: it is killed, its bound ends the process, `finish` on its stdin ends the pass, or `stop` there drains the worker. Its
#: records go over OTLP to a Collector that takes the connection and never answers, as one behind a cut does, so an end
#: that waited for its flush would outlive the worker's hold on its task.
_DOOMED = f"""
import asyncio, logging, socket, sys
from pathlib import Path
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from maintenance.services.queue import TaskQueue, Worker

silent = socket.socket()
silent.bind(("127.0.0.1", 0))
silent.listen()
provider = LoggerProvider()
collector = f"http://127.0.0.1:{{silent.getsockname()[1]}}/v1/logs"
provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=collector, timeout=60)))
set_logger_provider(provider)
logging.getLogger().addHandler(LoggingHandler(logger_provider=provider))

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
    """Raised where an in-process replica's worker would end its process (`lakehouse.supervision.end_process`)."""


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
            end=self._end_process,
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

    def task_loop_failure(self) -> BaseException | None:
        """What ended the replica's task loop by an error, which a replica's supervisor ends the process on; none while
        the loop runs, or once it was stopped."""
        tasks_loop = self._loops[0]
        return tasks_loop.exception() if tasks_loop.done() and not tasks_loop.cancelled() else None

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


def _replica_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broker: Broker) -> MaintenanceSettings:
    """A replica's settings as the service reads them: a secret store it logs in to as itself, an object-store identity
    that is its ServiceAccount token as the pod's is, and the store session its lifespan exchanges that token for already
    held: no test here reads an object."""
    (token := tmp_path / "token").write_text("a projected token")
    monkeypatch.setenv("MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("MAINTENANCE_S3_STS_ENDPOINT", "http://store.example:9000")
    # Where a replica's secret store is said to be; the suite's double answers there (`_start_replica`).
    monkeypatch.setenv("RASK_SECRET_STORE_ADDR", SecretStoreDouble.ADDRESS)
    monkeypatch.setenv("RASK_SECRET_STORE_ROLE", SecretStoreDouble.ROLE)
    settings = MaintenanceSettings.model_validate(
        {
            **{"s3_bucket": "b", "nats_url": broker.url, "drain_seconds": 5},
            **{"task_heartbeat_seconds": _HEARTBEAT, "task_ack_wait_seconds": _ACK_WAIT, "task_max_deliver": _MAX_DELIVER},
        }
    )
    # The session `lakehouse.store_session.start_store_session` holds, which the store-events reader signs with.
    sessions = store_session.SessionCache()
    monkeypatch.setattr(store_session, "SESSIONS", sessions)
    identity = settings.web_identity
    assert identity is not None, "the service holds no web identity, so it would sign with no session"
    session = S3KeyPair(access_key_id="TESTSESSION", secret_access_key=SecretStr("test-only"), session_token=SecretStr("test-only"))
    sessions.hold(identity, session, asked_at=sessions.now(), lifetime_seconds=3600)
    return settings


async def _start_replica(stack: AsyncExitStack, settings: MaintenanceSettings, worker: Credential, *, end: EndProcess = end_process) -> TaskQueue:
    """A replica started as the service starts it (`tasks.running_worker`), its bus credential read from a stand-in of the
    store's secrets API, run until ``stack`` closes; the queue it runs on. Every task it is handed runs nothing."""
    # Each replica logs in for itself, as each pod does: the process's one session is forgotten first.
    secrets.forget_secret_store()
    store = SecretStoreDouble()
    credential = store.hold(settings.nats_credentials_secret, {"jwt": worker.jwt, "seed": worker.seed()})
    with store.router:
        lane = await stack.enter_async_context(tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter(), end=end))
        # Read while the router answers: stopping it rolls each route's calls back to what they were when it started.
        asked = (store.login.call_count, credential.call_count)
    assert asked == (1, 1), "the replica did not read its bus credential under a login of its own"
    assert lane is not None, "the replica started with no bus"
    return lane


async def _runs_nothing(_task: Task, **_: object) -> None:
    return None


async def _raises(_worker: Worker) -> None:
    raise RuntimeError("a fault no loop handles")


async def _returns(_worker: Worker) -> None:
    return None


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


async def _purge_the_claims(rig: Client, _monkeypatch: pytest.MonkeyPatch) -> None:
    purged = await rig.request("$JS.API.STREAM.PURGE.KV_MAINTENANCE_CLAIMS")
    assert purged is not None and purged.get("success") is True, f"the claims were not purged: {purged}"


async def _break_the_renewal(_rig: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fault(self: TaskQueue, key: str, token: str, revision: int, *, within: float) -> int | None:
        raise LookupError(f"a fault no heartbeat handles, renewing {key}")

    monkeypatch.setattr(TaskQueue, "renew", fault)


class _Bus:
    """The module's bus: the cluster, the users minted for it, the render its stream Job came from, and what the servers
    had logged when the running test began, so a test reads only its own lines."""

    def __init__(self, tools: Tools, docs: tuple[dict[str, Any], ...], mint_: Mint, broker: Broker, home: Path) -> None:
        self.tools, self.docs, self.mint, self.broker, self.home = tools, docs, mint_, broker, home
        self._before: Counter[str] = Counter()

    @property
    def worker(self) -> Credential:
        return self.mint.credentials[_WORKER]

    async def rig(self) -> Client:
        return await Client.connect(self.broker.url, self.mint.credentials[_RIG], _RIG, answer_within=10.0)

    def declare(self) -> None:
        """The chart's stream Job, run as `admin` (`_declare_the_queue`)."""
        _declare_the_queue(self.tools, self.docs, self.broker, self.mint.credentials["admin"], self.home)

    async def declare_afresh(self) -> None:
        """Delete the maintenance streams and the claims bucket, then have the stream Job declare them again: no message,
        delivery count, consumer state or claim survives into the next test."""
        rig = await self.rig()
        try:
            for stream in (TASKS_STREAM, SCHEDULES_STREAM, STORE_EVENTS_STREAM, f"KV_{CLAIMS_BUCKET}"):
                await rig.request(f"$JS.API.STREAM.DELETE.{stream}")
        finally:
            await rig.close()
        await asyncio.to_thread(self.declare)
        self._before = Counter(self.broker.log_text().splitlines())

    def logged_since(self) -> str:
        """What the servers logged since the running test began. Each line carries its time to the microsecond, so a line
        logged before is told from a new one by its text."""
        return "\n".join((Counter(self.broker.log_text().splitlines()) - self._before).elements())

    def refusals(self) -> list[Violation]:
        """What the broker refused the worker's user since the running test began: a call the chart's table does not
        grant."""
        return [v for v in violations(self.logged_since()) if self.mint.user_of(v.user_key) == _WORKER]


@pytest.fixture(scope="module")
def bus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Bus]:
    home = tmp_path_factory.mktemp("bus")
    tools, docs, mint_, broker = _the_bus(home, {_RIG: _RIG_GRANTS})
    try:
        yield _Bus(tools, docs, mint_, broker, home)
    finally:
        broker.stop()


@pytest_asyncio.fixture
async def declared(bus: _Bus) -> AsyncIterator[_Bus]:
    """The module's bus with its queue freshly declared; a call the broker refused the worker fails the test."""
    await bus.declare_afresh()
    yield bus
    assert bus.refusals() == [], "the broker refused the worker a call the chart must grant"


class _Pulls:
    """Every pull a worker makes, as the rig sees it go by: its subject, its reply inbox, and when it was seen."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, float]] = []

    async def record(self, msg: Msg) -> None:
        self.seen.append((msg.subject, msg.reply, asyncio.get_running_loop().time()))

    async def next_after(self, count: int, seconds: float) -> float:
        """When the first pull after the ``count``-th was seen, waiting up to ``seconds`` for it."""
        assert await _until(lambda: len(self.seen) > count, seconds), "no worker pulled"
        return self.seen[count][2]


class _Fleet:
    """What one test starts on the bus: the commit lane's connection, in-process replicas, workers in processes of their
    own, the rig's connection; each stopped at the test's end."""

    def __init__(self, bus: _Bus) -> None:
        self.bus = bus
        self.replicas: list[_Replica] = []
        self.processes: list[asyncio.subprocess.Process] = []
        self.queues: list[TaskQueue] = []
        self.clients: list[Client] = []
        self.links: list[Link] = []
        #: Loops the test runs itself, cancelled first.
        self.on_stop: list[asyncio.Task[None]] = []

    async def lane(self, name: str = "commit-lane") -> TaskQueue:
        queue = await TaskQueue.connect(self.bus.broker.url, self.bus.worker.creds_file.read_text(), name=name)
        self.queues.append(queue)
        return queue

    async def start(self, name: str, runs: _Runs, *, ready: Callable[[], bool] = lambda: True, url: str | None = None) -> _Replica:
        replica = await _Replica.start(url or self.bus.broker.url, self.bus.worker, name, runs, ready=ready)
        self.replicas.append(replica)
        return replica

    async def doomed(self, max_pass_seconds: float, url: str | None = None) -> asyncio.subprocess.Process:
        process = await _doomed(url or self.bus.broker.url, self.bus.worker, max_pass_seconds)
        self.processes.append(process)
        return process

    async def rig(self) -> Client:
        client = await self.bus.rig()
        self.clients.append(client)
        return client

    async def link(self) -> Link:
        link = Link(self.bus.broker.port)
        await link.start()
        self.links.append(link)
        return link

    async def pulls(self) -> _Pulls:
        """The rig, watching every pull for a task."""
        pulls = _Pulls()
        rig = await self.rig()
        await rig.nc.subscribe(f"$JS.API.CONSUMER.MSG.NEXT.{TASKS_STREAM}.{TASK_CONSUMER}", cb=pulls.record)
        await rig.settle()
        return pulls

    def ended(self) -> dict[str, list[str]]:
        """The in-process replicas that ended where a worker ends its process, and why."""
        return {replica.name: replica.ended for replica in self.replicas if replica.ended}

    async def stop(self) -> None:
        for running in self.on_stop:
            running.cancel()
        await asyncio.gather(*self.on_stop, return_exceptions=True)
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        for replica in self.replicas:
            await replica.stop()
        for client in self.clients:
            await client.close()
        for queue in self.queues:
            await queue.close()
        for link in self.links:
            await link.stop()


@pytest_asyncio.fixture
async def fleet(declared: _Bus) -> AsyncIterator[_Fleet]:
    made = _Fleet(declared)
    try:
        yield made
    finally:
        await made.stop()


def _task(uuid: str, kind: TaskKind = "table") -> Task:
    return Task(kind=kind, uuid=uuid, location=f"s3://bucket/{uuid}")


# --- a worker's hold on its task --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_past_twice_its_bound_ends_its_workers_process(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    assert await lane.enqueue(_task("hung")) is True, fleet.bus.refusals()
    hung = await fleet.doomed(1)
    assert await _said(hung) == ["running", "hung"], f"a worker did not take the task; refused: {fleet.bus.refusals()}"

    ended = await _until(lambda: hung.returncode is not None, 30)

    assert ended and hung.returncode == _HUNG_EXIT, f"a pass past twice its bound did not end its worker's process (exit {hung.returncode})"


async def _outlived_every_delivery(fleet: _Fleet) -> tuple[TaskQueue, _Runs, bool]:
    """A table task whose pass outlived its worker on each of its deliveries, the next delivery handed to a worker that
    would run it: the commit lane, that worker's runs, and whether the task stopped holding its table."""
    lane = await fleet.lane()
    assert await lane.enqueue(_task("killer")) is True, fleet.bus.refusals()
    for _ in range(_MAX_DELIVER):
        hung = await fleet.doomed(1)
        assert await _said(hung) == ["running", "killer"], f"a worker did not take the task its table held; refused: {fleet.bus.refusals()}"
        assert await _until(lambda hung=hung: hung.returncode is not None, 30), "a pass past twice its bound did not end its worker's process"
    runs = _Runs()
    await fleet.start("next", runs)

    async def killer_ended() -> bool:
        return "killer" not in await lane.held("task", "table")

    return lane, runs, await _until(killer_ended, _ACK_WAIT + 15)


@pytest.mark.asyncio
async def test_a_task_that_outlived_its_worker_on_every_delivery_is_ended_unrun(fleet: _Fleet) -> None:
    _, runs, ended = await _outlived_every_delivery(fleet)

    assert (ended, runs.times("killer")) == (True, 0), f"a task past its last delivery still held its table, or was run: {runs.ran}"


@pytest.mark.asyncio
async def test_a_task_that_outlived_its_worker_on_every_delivery_holds_its_tables_next_try(fleet: _Fleet) -> None:
    lane, _, _ = await _outlived_every_delivery(fleet)

    assert "killer" in await lane.held("schedule", "table"), "an ended task's table holds no next try, so the hourly audit hands it back"


@pytest.mark.asyncio
async def test_a_task_put_late_in_its_workers_pull_stays_with_it_while_it_beats(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    pulls = await fleet.pulls()
    dier = await fleet.doomed(600)
    # The task arrives late in the doomed worker's pull, as one put on an idle queue arrives: timed from the pull itself.
    pulled_at = await pulls.next_after(0, 15)
    await asyncio.sleep(max(0.0, pulled_at + _LATE_IN_PULL - asyncio.get_running_loop().time()))
    assert await lane.enqueue(_task("dying")) is True, fleet.bus.refusals()
    assert await _said(dier) == ["running", "dying"], f"the doomed worker did not take the task; refused: {fleet.bus.refusals()}"
    runs = _Runs()
    await fleet.start("second", runs)

    # Three ack waits in which the task must stay with the worker that still beats for it: nothing to poll for.
    await asyncio.sleep(3 * _ACK_WAIT)

    assert runs.times("dying") == 0, "a live worker's task went to another: its heartbeat did not hold it"


@pytest.mark.asyncio
async def test_a_second_task_for_a_table_whose_task_is_in_flight_is_refused(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    runs = _Runs()
    runs.held["in-flight"] = asyncio.Event()
    await fleet.start("first", runs)
    assert await lane.enqueue(_task("in-flight")) is True, fleet.bus.refusals()
    assert await _until(lambda: runs.times("in-flight") == 1, 15), f"the task never ran; refused: {fleet.bus.refusals()}"

    again = await lane.enqueue(_task("in-flight"))

    assert again is False, "the queue took a second task for a table whose task is in flight"


@pytest.mark.asyncio
async def test_a_killed_workers_task_goes_to_another_worker(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    dier = await fleet.doomed(600)
    assert await lane.enqueue(_task("dying")) is True, fleet.bus.refusals()
    assert await _said(dier) == ["running", "dying"], f"the doomed worker did not take the task; refused: {fleet.bus.refusals()}"
    runs = _Runs()
    await fleet.start("second", runs)

    dier.kill()
    await dier.wait()

    assert await _until(lambda: runs.times("dying") == 1, _ACK_WAIT + 15), f"a dead worker's task never reached the other; refused: {fleet.bus.refusals()}"


@pytest.mark.asyncio
async def test_a_worker_the_bus_stops_answering_mid_pass_ends_its_process_before_its_task_runs_elsewhere(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    link = await fleet.link()
    stranded = await fleet.doomed(600, url=link.url)
    assert await lane.enqueue(_task("cut")) is True, fleet.bus.refusals()
    assert await _said(stranded) == ["running", "cut"], f"the stranded worker did not take the task; refused: {fleet.bus.refusals()}"
    runs = _Runs()
    runs.at_start["cut"] = lambda: stranded.returncode
    await fleet.start("second", runs)

    outage = asyncio.create_task(link.cut(4 * _ACK_WAIT))
    assert await _until(lambda: link.down, 5), "the link was never cut"
    # Its last heartbeat was answered before the cut, so its lease runs out within one ack wait of it.
    ended_itself = await _until(lambda: stranded.returncode is not None, _ACK_WAIT)
    moved = await _until(lambda: runs.times("cut") == 1, _ACK_WAIT + 15)
    await outage

    assert ended_itself and stranded.returncode == _HUNG_EXIT, f"a worker the bus stopped answering ran its pass past its lease (exit {stranded.returncode})"
    assert moved, f"a stranded worker's task never reached the other; refused: {fleet.bus.refusals()}"
    assert runs.seen["cut"] == _HUNG_EXIT, "the task went to another worker while the one the bus stopped answering still ran it"


@pytest.mark.asyncio
async def test_a_worker_whose_drain_ends_mid_pass_ends_its_process_before_its_task_runs_elsewhere(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    stopping = await fleet.doomed(600)
    assert await lane.enqueue(_task("drained")) is True, fleet.bus.refusals()
    assert await _said(stopping) == ["running", "drained"], f"the stopping worker did not take the task; refused: {fleet.bus.refusals()}"
    runs = _Runs()
    runs.at_start["drained"] = lambda: stopping.returncode
    await fleet.start("second", runs)
    assert stopping.stdin is not None

    stopping.stdin.write(b"stop\n")
    await stopping.stdin.drain()

    ended_itself = await _until(lambda: stopping.returncode is not None, _DOOMED_DRAIN + 5)
    assert ended_itself and stopping.returncode == _HUNG_EXIT, f"a worker whose drain ended mid-pass kept its pass (exit {stopping.returncode})"
    assert await _until(lambda: runs.times("drained") == 1, _ACK_WAIT + 15), f"a drained worker's task never reached the other; refused: {fleet.bus.refusals()}"
    assert runs.seen["drained"] == _HUNG_EXIT, "the task went to another worker while the one draining still ran it"


class _CutSettle(BaseModel):
    """What a pass that ended once the bus was cut left: its worker's exit, whether its task reached the other worker,
    and whether the queue took the task again once the bus was back."""

    exit: int | None
    moved: bool
    put_again: bool


async def _a_pass_ends_in_a_bus_cut(fleet: _Fleet) -> _CutSettle:
    lane = await fleet.lane()
    link = await fleet.link()
    settler = await fleet.doomed(600, url=link.url)
    assert await lane.enqueue(_task("settled")) is True, fleet.bus.refusals()
    assert await _said(settler) == ["running", "settled"], f"the settling worker did not take the task; refused: {fleet.bus.refusals()}"
    runs = _Runs()
    release = runs.held["settled"] = asyncio.Event()
    await fleet.start("second", runs)
    outage = asyncio.create_task(link.cut(4 * _ACK_WAIT))
    # The pass ends once the cut stands, so its ack cannot be answered.
    assert await _until(lambda: link.down, 5), "the link was never cut"
    assert settler.stdin is not None
    settler.stdin.write(b"finish\n")
    await settler.stdin.drain()
    await _until(lambda: settler.returncode is not None, 2 * _ACK_WAIT)
    moved = await _until(lambda: runs.times("settled") == 1, _ACK_WAIT + 15)
    await outage
    # The bus is back: an ack held through the cut must not have freed the task the other worker still runs.
    put_again = await lane.enqueue(_task("settled"))
    release.set()
    return _CutSettle(exit=settler.returncode, moved=moved, put_again=put_again)


@pytest.mark.asyncio
async def test_a_worker_whose_pass_ends_in_a_bus_cut_ends_its_process(fleet: _Fleet) -> None:
    settled = await _a_pass_ends_in_a_bus_cut(fleet)

    assert settled.exit == _HUNG_EXIT, f"a worker whose pass ended in a bus cut kept its ack for after it (exit {settled.exit})"


@pytest.mark.asyncio
async def test_no_ack_held_through_a_bus_cut_frees_the_task_another_worker_runs(fleet: _Fleet) -> None:
    """The task must have reached the other worker for its being refused a second put to say the ack did not free it."""
    settled = await _a_pass_ends_in_a_bus_cut(fleet)

    assert (settled.moved, settled.put_again) == (True, False), f"the task never moved, or an ack sent once the bus came back freed it: {settled}"


@pytest.mark.asyncio
async def test_a_task_delivered_as_its_workers_pull_runs_out_runs_on_that_delivery(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    """A pull's wait at the server counts from the request's arrival, and the server goes on answering it until the
    worker's unsubscribe arrives too, so on a slow path to the stream's leader the server holds a pull past the moment
    its worker stopped waiting, by the time the path takes. A task sent in that window answers a pull its worker must
    still be reading: run then, and not handed on an ack wait later on a second delivery. The link slows every frame the
    worker sends by `_PULL_TRANSIT`, and the task is put halfway through the window that opens."""
    pulls = await fleet.pulls()
    link = await fleet.link()
    link.lag(_PULL_TRANSIT)
    lane = await fleet.lane()
    runs = _Runs()
    await fleet.start("puller", runs, url=link.url)
    arrived = await pulls.next_after(len(pulls.seen), 15)
    # Placed in time, not waited for: the server holds this pull until `arrived + _FETCH_SECONDS`, and its worker, which
    # sent it `_PULL_TRANSIT` earlier, would stop waiting `_PULL_TRANSIT` before that.
    await asyncio.sleep(arrived + _FETCH_SECONDS - _PULL_TRANSIT / 2 - asyncio.get_running_loop().time())

    assert await lane.enqueue(_task("in-transit")) is True, fleet.bus.refusals()

    assert await _until(lambda: runs.times("in-transit") == 1, _ACK_WAIT + 15), f"the task never ran: {runs.ran}"
    assert maintenance_metrics.by("maintenance.tasks.redelivered", "kind") == {}, (
        "the task ran only on a second delivery: its first went to a pull its worker had stopped reading"
    )


@pytest.mark.asyncio
async def test_a_task_delivered_to_a_pull_its_worker_gave_up_on_runs_once_on_the_worker_that_pulls_next(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    pulls = await fleet.pulls()
    runs = _Runs()
    release_first = runs.held["first"] = asyncio.Event()
    release_late = runs.held["late"] = asyncio.Event()
    beta_ready = asyncio.Event()
    await fleet.start("alpha", runs)
    await fleet.start("beta", runs, ready=beta_ready.is_set)
    assert await lane.enqueue(_task("first")) is True
    assert await _until(lambda: runs.times("first") == 1, 15), f"the first task never ran; refused: {fleet.bus.refusals()}"
    # Alpha's pull its running pass answered, replayed as a reconnect replays a pull it held back; the next task answers it.
    rig = await fleet.rig()
    await rig.nc.publish(pulls.seen[-1][0], json.dumps({"batch": 1, "expires": 30_000_000_000}).encode(), reply=pulls.seen[-1][1])
    await rig.settle()
    assert await lane.enqueue(_task("late")) is True
    beta_ready.set()
    assert await _until(lambda: runs.times("late") == 1, _ACK_WAIT + 15), f"a task delivered to a pull its worker gave up on was never handed on: {runs.ran}"

    release_first.set()
    # Alpha pulls again once its pass ends, and must not run the task its old pull was answered with: nothing to poll for.
    await asyncio.sleep(_ACK_WAIT)
    release_late.set()

    late = [name for name, uuid in runs.ran if uuid == "late"]
    assert late == ["beta"], f"a task ran off a delivery to a pull its worker had given up on, beside its redelivery: {late}"
    assert fleet.ended() == {}, f"an in-process replica ended where a worker ends its process: {fleet.ended()}"


@pytest.mark.asyncio
async def test_a_late_ack_that_frees_a_live_passs_task_lets_no_second_pass_of_its_table_run_beside_it(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    rig = await fleet.rig()
    deliveries: list[Msg] = []

    async def delivered(msg: Msg) -> None:
        if msg.subject.endswith(".fenced"):
            deliveries.append(msg)

    await rig.nc.subscribe(f"{INBOX_PREFIX}.>", cb=delivered)
    await rig.settle()
    runs = _Runs()
    release = runs.held["fenced"] = asyncio.Event()
    await fleet.start("alpha", runs)
    assert await lane.enqueue(_task("fenced")) is True
    assert await _until(lambda: runs.times("fenced") == 1 and bool(deliveries), 15), f"the fenced task never ran; refused: {fleet.bus.refusals()}"
    # A late ack on the delivery the live pass holds, as one the network held past a stale worker's lease lands.
    await rig.nc.publish(deliveries[0].reply, b"+ACK")
    await rig.settle()

    async def freed() -> bool:
        return "fenced" not in await lane.held("task", "table")

    assert await _until(freed, 10), "a late ack did not free the task a live pass holds"
    assert await lane.enqueue(_task("fenced")) is True, "the queue refused a task for a table whose task a late ack freed"
    # The next task's delivery comes at once, and must wait out the live pass's claim: nothing to poll for.
    await asyncio.sleep(2 * _ACK_WAIT)
    beside = runs.times("fenced")
    release.set()

    assert beside == 1, f"a pass ran beside another of its table once a late ack freed its task: {runs.ran}"
    assert await _until(lambda: runs.times("fenced") == 2, _ACK_WAIT + 30), f"the table's next task never ran once its pass ended: {runs.ran}"
    assert fleet.ended() == {}, f"an in-process replica ended where a worker ends its process: {fleet.ended()}"


# --- bursts, and a table's tasks put at once --------------------------------------------------------------------------

_BURST = [_task(f"burst-{n}") for n in range(6)]


@pytest.mark.asyncio
async def test_a_burst_of_follow_ups_holds_one_per_table(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    due = datetime.now(UTC) + timedelta(hours=1)

    held = [sorted(await asyncio.gather(*(lane.follow_up(task, due) for _ in range(3)), return_exceptions=True), key=repr) for task in _BURST]

    assert held == [[False, False, True]] * len(_BURST), f"a burst held other than one follow-up per table: {held}"


@pytest.mark.asyncio
async def test_each_follow_up_of_a_burst_runs_once(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    runs = _Runs()
    await fleet.start("first", runs)
    await fleet.start("second", runs)
    due = datetime.now(UTC) + timedelta(seconds=2)
    for task in _BURST:
        assert await lane.follow_up(task, due) is True
    assert await _until(lambda: all(runs.times(task.uuid) for task in _BURST), 30), f"the burst's passes did not all run: {runs.ran}"

    # An ack wait past the last pass, in which no pass may run a second time: nothing to poll for.
    await asyncio.sleep(_ACK_WAIT + 1)

    assert Counter(uuid for _, uuid in runs.ran) == Counter(task.uuid for task in _BURST), f"a burst's pass ran other than once: {runs.ran}"


async def _a_held_burst(fleet: _Fleet) -> list[tuple[str, str]]:
    """Two ready workers and one not ready, and a burst of follow-ups each ready worker holds the first pass of, so the
    burst's second pass can only go to the other ready worker: the share is fixed by the holds, not by which pull the bus
    answers first. Which worker took which of the first two passes, once every pass of the burst has run."""
    lane = await fleet.lane()
    runs = _Runs()
    released = asyncio.Event()
    for task in _BURST:
        runs.held[task.uuid] = released
    await fleet.start("first", runs)
    await fleet.start("second", runs)
    await fleet.start("not-ready", runs, ready=lambda: False)
    due = datetime.now(UTC) + timedelta(seconds=2)
    for task in _BURST:
        assert await lane.follow_up(task, due) is True
    assert await _until(lambda: len(runs.ran) == 2, 30), f"the two ready workers did not each take a pass: {runs.ran}; refused: {fleet.bus.refusals()}"
    holding = list(runs.ran)
    released.set()
    assert await _until(lambda: all(runs.times(task.uuid) for task in _BURST), 30), f"the burst's passes did not all run: {runs.ran}"
    return holding + runs.ran[2:]


@pytest.mark.asyncio
async def test_two_ready_workers_share_a_burst(fleet: _Fleet) -> None:
    ran = await _a_held_burst(fleet)

    assert {name for name, _ in ran[:2]} == {"first", "second"}, f"the two ready workers did not share the burst: {ran}"


@pytest.mark.asyncio
async def test_a_worker_that_is_not_ready_takes_no_pass_of_a_burst(fleet: _Fleet) -> None:
    ran = await _a_held_burst(fleet)

    assert "not-ready" not in {name for name, _ in ran}, f"a worker that was not ready took a pass: {ran}"


@pytest.mark.asyncio
async def test_three_puts_of_one_tables_task_at_once_queue_one(fleet: _Fleet) -> None:
    lane = await fleet.lane()

    put = sorted(await asyncio.gather(*(lane.enqueue(_task("busy")) for _ in range(3)), return_exceptions=True), key=repr)

    assert put == [False, False, True], f"three puts of one table's task at once queued other than one: {put}"


async def _a_follow_up_fires_while_its_tables_task_runs(fleet: _Fleet) -> tuple[TaskQueue, _Runs, int]:
    """A table's task held running while its follow-up fires: the commit lane, the runs, and how many passes of the
    table had run by the time the task was let finish."""
    lane = await fleet.lane()
    runs = _Runs()
    release = runs.held["busy"] = asyncio.Event()
    await fleet.start("first", runs)
    assert await lane.enqueue(_task("busy")) is True
    assert await _until(lambda: runs.times("busy") == 1, 15), f"the busy table's task never ran; refused: {fleet.bus.refusals()}"
    assert await lane.follow_up(_task("busy"), datetime.now(UTC) + timedelta(seconds=1)) is True
    # The follow-up fires inside these seconds while the task still holds its table, and must not pass it: nothing to poll for.
    await asyncio.sleep(4)
    beside = runs.times("busy")
    release.set()
    return lane, runs, beside


@pytest.mark.asyncio
async def test_a_follow_up_that_fires_while_its_tables_task_runs_does_not_pass_the_table_beside_it(fleet: _Fleet) -> None:
    _, _, beside = await _a_follow_up_fires_while_its_tables_task_runs(fleet)

    assert beside == 1, "a follow-up was passed while a task still held its table"


@pytest.mark.asyncio
async def test_a_follow_up_that_fires_while_its_tables_task_runs_passes_the_table_once_the_task_is_done(fleet: _Fleet) -> None:
    """Done means the follow-up's pass ran and its table is held by nothing after it."""
    lane, runs, _ = await _a_follow_up_fires_while_its_tables_task_runs(fleet)

    async def passed_and_freed() -> bool:
        return runs.times("busy") == 2 and not await lane.held("task", "table")

    assert await _until(passed_and_freed, _FOLLOW_UP_WAIT), f"the follow-up was never passed, or its table stayed held: {runs.ran}"


# --- claims -----------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_and_its_renewal_asked_again_find_their_own_token_standing(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    mine = await lane.claim("table.asked-again", "mine", within=10)
    assert mine is not None, "the commit lane's connection could not claim a free table"
    renewed = await lane.renew("table.asked-again", "mine", mine, within=10)
    assert renewed is not None, "a claim's holder could not renew it"

    again = (await lane.renew("table.asked-again", "mine", mine, within=10), await lane.claim("table.asked-again", "mine", within=10))

    assert again == (renewed, renewed), "a renewal or a claim asked again after its answer was lost was refused"


@pytest.mark.asyncio
async def test_a_claim_another_holds_is_neither_taken_nor_renewed(fleet: _Fleet) -> None:
    """A stale holder's renewal names a revision the holder's own renewal has since moved."""
    lane = await fleet.lane()
    mine = await lane.claim("table.held", "mine", within=10)
    assert mine is not None, "the commit lane's connection could not claim a free table"
    assert await lane.renew("table.held", "mine", mine, within=10) is not None, "a claim's holder could not renew it"

    taken = (await lane.renew("table.held", "theirs", mine, within=10), await lane.claim("table.held", "theirs", within=10))

    assert taken == (None, None), "a stale renewal or a claim took a claim another holds"


@pytest.mark.asyncio
async def test_a_release_naming_a_revision_a_renewal_moved_frees_its_holders_claim(fleet: _Fleet) -> None:
    """As when the renewal's answer was lost, the release names the revision before it; it finds its own token standing."""
    lane = await fleet.lane()
    mine = await lane.claim("table.released", "mine", within=10)
    assert mine is not None, "the commit lane's connection could not claim a free table"
    assert await lane.renew("table.released", "mine", mine, within=10) is not None, "a claim's holder could not renew it"

    await lane.release("table.released", "mine", mine)

    assert await lane.claim("table.released", "theirs", within=10) is not None, "a release refused for a moved revision left its holder's claim standing"


@pytest.mark.asyncio
async def test_a_release_by_a_former_holder_leaves_the_current_holders_claim(fleet: _Fleet) -> None:
    lane = await fleet.lane()
    mine = await lane.claim("table.passed-on", "mine", within=10)
    assert mine is not None, "the commit lane's connection could not claim a free table"
    await lane.release("table.passed-on", "mine", mine)
    theirs = await lane.claim("table.passed-on", "theirs", within=10)
    assert theirs is not None, "a released claim could not be taken"

    await lane.release("table.passed-on", "mine", mine)

    assert await lane.claim("table.passed-on", "mine", within=10) is None, "a release by a former holder freed the claim another holds"


# --- a replica started as the service starts it -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", [pytest.param(_raises, id="with-an-error"), pytest.param(_returns, id="without-one")])
async def test_a_loop_that_ends_ends_its_replica(
    declared: _Bus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: Callable[[Worker], Awaitable[None]]
) -> None:
    """A replica's loop that ends before the replica stops ends its process, whether it raised or returned: a replica whose
    loop is gone stays Ready with that loop's work undone, and only a new container repairs it (ADR 0203)."""
    ended: list[tuple[str, object]] = []

    def record_the_end(event: str, **extra: object) -> None:
        ended.append((event, extra.get("loop")))

    monkeypatch.setattr(tasks, "run_task", _runs_nothing)
    monkeypatch.setattr(Worker, "run_due", loop)
    settings = _replica_settings(tmp_path, monkeypatch, declared.broker)
    async with AsyncExitStack() as stack:
        await _start_replica(stack, settings, declared.worker, end=cast("EndProcess", record_the_end))
        assert await _until(lambda: bool(ended), 20), "the loop moving fired schedules ended and its replica went on"
    assert ended == [("background_work_ended", "maintenance-due")]


_CADENCES = [Task(kind="audit").subject("schedule"), Task(kind="scan").subject("schedule")]


async def _cadences_held(reader: Client) -> list[bool]:
    answers = [await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": subject}) or {} for subject in _CADENCES]
    return ["message" in answer for answer in answers]


@pytest.mark.asyncio
async def test_a_replica_starts_with_the_audits_and_the_scans_schedules(fleet: _Fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "run_task", _runs_nothing)
    settings = _replica_settings(tmp_path, monkeypatch, fleet.bus.broker)
    reader = await fleet.rig()
    async with AsyncExitStack() as stack:
        await _start_replica(stack, settings, fleet.bus.worker)
        held = await _cadences_held(reader)
    assert held == [True, True], "a replica started without the audit's and the scan's schedules"


@pytest.mark.asyncio
async def test_a_replica_puts_back_the_schedules_the_bus_lost(fleet: _Fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "run_task", _runs_nothing)
    monkeypatch.setattr(tasks, "_CADENCE_SECONDS", 2.0)
    settings = _replica_settings(tmp_path, monkeypatch, fleet.bus.broker)
    reader = await fleet.rig()
    async with AsyncExitStack() as stack:
        await _start_replica(stack, settings, fleet.bus.worker)
        assert await _until(lambda: _all_held(reader), 10), "a replica started without the audit's and the scan's schedules"
        for subject in _CADENCES:
            await reader.request(f"$JS.API.STREAM.PURGE.{SCHEDULES_STREAM}", {"filter": subject})

        put_back = await _until(lambda: _all_held(reader), 10)
    assert put_back, "the schedules the bus lost were never put back"


async def _all_held(reader: Client) -> bool:
    return await _cadences_held(reader) == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("through_a_cut", [pytest.param(False, id="the-bus-ends-its-session"), pytest.param(True, id="the-bus-refuses-it-at-a-reconnect")])
async def test_a_worker_whose_credential_the_bus_takes_no_longer_ends_itself(fleet: _Fleet, tmp_path: Path, through_a_cut: bool) -> None:
    """nats-py keeps the credential a worker read at start: it closes for good on a session the server ends, and retries a
    credential the server refuses for ever. So a worker the bus takes its credential from no longer ends its process, and
    its pod, started again, reads the credential the store then holds. A connection held while its credential runs out
    is ended by the server; one cut through a `Link` until the credential has run out is refused when it reconnects."""
    bus = fleet.bus
    publish, subscribe = nats_users(bus.docs)[_WORKER]
    grants = Permissions(publish=tuple(sorted(publish)), subscribe=tuple(sorted(subscribe)), expiry="1m")
    credential = issue(bus.tools.nsc, bus.home / "nsc", f"{_EXPIRING}-{'cut' if through_a_cut else 'held'}", grants)
    link = await fleet.link()
    log = tmp_path / "holding.log"
    holding = await _holding(link.url if through_a_cut else bus.broker.url, credential, log)
    fleet.processes.append(holding)
    assert await _said(holding) == ["connected"], "a worker could not connect with its own credential"

    if through_a_cut:
        await link.cut(_EXPIRY + 15)
        ended = await _until(lambda: holding.returncode is not None, 30)
    else:
        ended = await _until(lambda: holding.returncode is not None, _EXPIRY + 30)

    assert ended and holding.returncode == _HUNG_EXIT, (
        f"a worker whose credential the bus took no longer kept its process (exit {holding.returncode}): {log.read_text()[-600:]}"
    )


# --- store events -----------------------------------------------------------------------------------------------------


class _StoreEvents:
    """The service's own store-event handling (`maintenance.services.store_events.StoreEvents`) behind one worker, on a
    control root of the test's own, where a claim the store cannot read is a directory standing at its key. The worker
    ends an event on the third delivery, which the consumer the render declares at `_MAX_DELIVER` allows (one past it)."""

    def __init__(self, fleet: _Fleet, control: Path) -> None:
        self.fleet = fleet
        self.control = control
        self.claims = location_claims.ClaimStore(control_root=str(control))
        self.handled: list[float] = []
        self._loops: list[asyncio.Task[None]] = []

    def claim(self, table: str, *, readable: bool) -> None:
        location_claims.take(
            self.claims, f"s3://b/{table}", f"lance_catalog$db${table}", ["lance_catalog", "db", table], is_gone=lambda _claim: False, uuid=table
        )
        if not readable:
            key = self.control / location_claims.claim_key(f"s3://b/{table}")
            key.unlink()
            key.mkdir()

    def make_readable(self, table: str) -> None:
        (self.control / location_claims.claim_key(f"s3://b/{table}")).rmdir()
        location_claims.take(
            self.claims, f"s3://b/{table}", f"lance_catalog$db${table}", ["lance_catalog", "db", table], is_gone=lambda _claim: False, uuid=table
        )

    async def deliver(self, body: bytes) -> None:
        queue = await self.fleet.lane("store-events")
        replica = StoreEvents(MaintenanceSettings(s3_bucket="b", control_root=str(self.control), commit_pass_delay_seconds=3600), queue)
        loop = asyncio.get_running_loop()

        async def handle(event: bytes) -> int:
            self.handled.append(loop.time())
            return await replica.hold_passes(event)

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
        self._loops.append(asyncio.create_task(worker.run_store_events()))
        store = await Client.connect(self.fleet.bus.broker.url, self.fleet.bus.mint.credentials[_STORE], _STORE)
        self.fleet.clients.append(store)
        await store.nc.publish("store.events", body)
        await store.settle()

    async def settled_after(self, deliveries: int, seconds: float) -> bool:
        """Whether the event was handled ``deliveries`` times and its stream holds nothing, within ``seconds``."""
        reader = await self.fleet.rig()

        async def settled() -> bool:
            state = (await reader.request(f"$JS.API.STREAM.INFO.{STORE_EVENTS_STREAM}") or {}).get("state", {})
            return len(self.handled) == deliveries and state.get("messages") == 0

        return await _until(settled, seconds)

    async def follow_up_held(self, table: str) -> bool:
        reader = await self.fleet.rig()
        subject = Task(kind="table", uuid=table).follow_up_subject("schedule")
        return _held_at(await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": subject})) is not None

    def waits(self) -> list[int]:
        return [round(later - earlier) for earlier, later in pairwise(self.handled)]

    async def stop(self) -> None:
        for running in self._loops:
            running.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)


@pytest_asyncio.fixture
async def store_events(fleet: _Fleet, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_StoreEvents]:
    # The delay is drawn at random beneath a bound that doubles (`redelivery_delay`, whose draw has a test of its own);
    # drawn here at 6 s a delivery, each gap names the delivery it followed.
    monkeypatch.setattr(worker_queue, "redelivery_delay", lambda delivered: 6.0 * delivered)
    control = tmp_path / "control"
    control.mkdir()
    made = _StoreEvents(fleet, control)
    try:
        yield made
    finally:
        await made.stop()


@pytest.mark.asyncio
async def test_a_store_event_whose_pass_is_held_is_acknowledged(store_events: _StoreEvents, maintenance_metrics: Measured) -> None:
    store_events.claim("held", readable=True)

    await store_events.deliver(_write_event("held/data/a.lance"))

    assert await store_events.settled_after(1, 30), f"the event was not settled on its first delivery: {store_events.handled}"
    assert await store_events.follow_up_held("held"), "the event's table holds no pass"
    assert maintenance_metrics.by("maintenance.store_events.settled", "outcome") == {"held": 1}


@pytest.mark.asyncio
async def test_a_store_event_that_is_no_store_event_is_ended_at_once(store_events: _StoreEvents, maintenance_metrics: Measured) -> None:
    await store_events.deliver(_write_event("held/data/b.lance", bucket=["b"]))

    assert await store_events.settled_after(1, 30), f"the event that is no store event was delivered again: {store_events.handled}"
    assert maintenance_metrics.by("maintenance.store_events.settled", "outcome") == {"unreadable": 1}


@pytest.mark.asyncio
async def test_a_store_event_whose_claim_the_store_refused_comes_back_after_its_retry_delay(store_events: _StoreEvents, maintenance_metrics: Measured) -> None:
    store_events.claim("flaky", readable=False)
    await store_events.deliver(_write_event("flaky/data/c.lance"))
    assert await _until(lambda: len(store_events.handled) == 1, 30), "the event the store refused was never handled"

    store_events.make_readable("flaky")

    assert await store_events.settled_after(2, 60), f"the event the store refused once was not settled: {store_events.handled}"
    assert 5 <= store_events.waits()[0] < 10, f"the refused event came back after {store_events.waits()} s, not after its retry delay"
    assert await store_events.follow_up_held("flaky"), "the event's table holds no pass once its claim was read"
    assert maintenance_metrics.by("maintenance.store_events.settled", "outcome") == {"retried": 1, "held": 1}


@pytest.mark.asyncio
async def test_a_store_event_the_store_refuses_on_every_delivery_is_ended_on_its_last(store_events: _StoreEvents, maintenance_metrics: Measured) -> None:
    store_events.claim("locked", readable=False)

    await store_events.deliver(_write_event("locked/data/d.lance"))

    assert await store_events.settled_after(3, 90), f"the event the store refused on every delivery was not ended: {store_events.handled}"
    waits = store_events.waits()
    assert 5 <= waits[0] < 10 and 11 <= waits[1] < 17, f"the refused event came back after {waits} s, not after each delivery's retry delay"
    assert not await store_events.follow_up_held("locked"), "an event that was ended held its table's pass"
    assert maintenance_metrics.by("maintenance.store_events.settled", "outcome") == {"retried": 2, "ended": 1}


# --- a task whose run fails -------------------------------------------------------------------------------------------


class _Panic(BaseException):
    """Where ``pyo3_runtime.PanicException`` sits: a BaseException that is no Exception and no request to stop."""


def _ends_its_process(event: str, **extra: object) -> NoReturn:
    raise AssertionError(f"the worker ended its process: {event} {extra}")


async def _failing_worker(fleet: _Fleet, failure: type[BaseException], runs: list[float]) -> TaskQueue:
    """A worker whose every run raises ``failure``, each run's time kept in ``runs``; the queue it pulls on."""
    queue = await fleet.lane("failing")
    loop = asyncio.get_running_loop()

    async def fails(task: Task) -> None:
        runs.append(loop.time())
        raise failure(f"the pass of {task.uuid} failed")

    replica = Worker(
        queue,
        run=fails,
        ready=lambda: True,
        heartbeat_seconds=_HEARTBEAT,
        ack_wait_seconds=_ACK_WAIT,
        max_deliver=_MAX_DELIVER,
        max_pass_seconds=600,
        retry_ended_seconds=3600,
        end=_ends_its_process,
    )
    running = asyncio.create_task(replica.run_tasks())
    fleet.on_stop.append(running)
    return queue


_FAILURES = [pytest.param(RuntimeError, id="an-error"), pytest.param(_Panic, id="a-native-panic")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _FAILURES)
async def test_a_task_whose_run_fails_comes_back_after_its_retry_delay(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]) -> None:
    # The delay is drawn at random beneath its bound; drawn here at 12 s, the gap between two runs is the rule's delay.
    monkeypatch.setattr(worker_queue, "redelivery_delay", lambda _delivered: 12.0)
    runs: list[float] = []
    queue = await _failing_worker(fleet, failure, runs)

    assert await queue.enqueue(_task("failing")) is True

    assert await _until(lambda: len(runs) == 2, 30), f"the failed task was not delivered again: {len(runs)} runs"
    assert 11.0 <= runs[1] - runs[0] < 17.0, f"the failed task came back after {runs[1] - runs[0]:.1f} s, not after its retry delay"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _FAILURES)
async def test_a_task_whose_run_fails_on_its_last_delivery_is_ended_with_its_failure_logged(
    fleet: _Fleet, maintenance_metrics: Measured, failure: type[BaseException], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker serves on: a run's failure, a native library's panic included, is the task's, never the process's."""
    caplog.set_level(logging.ERROR, logger=worker_queue.__name__)
    monkeypatch.setattr(worker_queue, "redelivery_delay", lambda _delivered: 1.0)
    runs: list[float] = []
    queue = await _failing_worker(fleet, failure, runs)

    assert await queue.enqueue(_task("failing")) is True

    async def ended() -> bool:
        return len(runs) == _MAX_DELIVER and not await queue.held("task", "table")

    assert await _until(ended, 30), f"the failing task was not ended on its last delivery: {len(runs)} runs"
    counted = {name: maintenance_metrics.by(f"maintenance.tasks.{name}", "kind") for name in ("retried", "redelivered", "ended", "completed")}
    assert counted == {"retried": {"table": _MAX_DELIVER - 1}, "redelivered": {"table": _MAX_DELIVER - 1}, "ended": {"table": 1}, "completed": {}}
    ended_by = [record.exc_info[0] for record in caplog.records if record.message == "maintenance_task_ended" and record.exc_info]
    assert ended_by == [failure], f"the ended task's failure was not logged with its traceback: {ended_by}"


@pytest.mark.asyncio
async def test_an_ended_tasks_next_try_is_held_a_recheck_out(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_queue, "redelivery_delay", lambda _delivered: 1.0)
    runs: list[float] = []
    queue = await _failing_worker(fleet, RuntimeError, runs)
    reader = await fleet.rig()
    failing = _task("failing")
    assert await queue.enqueue(failing) is True

    async def ended() -> bool:
        return len(runs) == _MAX_DELIVER and not await queue.held("task", "table")

    assert await _until(ended, 30), f"the failing task was not ended on its last delivery: {len(runs)} runs"
    ended_at = datetime.now(UTC)

    async def next_try() -> datetime | None:
        return _held_at(await reader.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": failing.subject("schedule")}))

    assert await _until(lambda: _is_held(next_try), 10), "the ended task's next try was never held"
    held_at = await next_try()
    assert held_at is not None and abs((held_at - ended_at).total_seconds() - 3600) < 10, f"the ended task's next try is held for {held_at}, not a recheck out"


@pytest.mark.asyncio
async def test_a_message_on_the_queue_that_is_no_task_is_ended_without_running(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    runs: list[float] = []
    await _failing_worker(fleet, RuntimeError, runs)
    writer = await Client.connect(fleet.bus.broker.url, fleet.bus.worker, "garbage", inbox=INBOX_PREFIX)
    fleet.clients.append(writer)

    await writer.nc.publish("maintenance.task.table.garbage", b"no task")
    await writer.settle()

    assert await _until(lambda: maintenance_metrics.by("maintenance.tasks.ended", "kind") == {"unreadable": 1}, 30), "the message that is no task was not ended"
    assert runs == [], "a message that is no task was run"


# --- stopping ---------------------------------------------------------------------------------------------------------


def _counting_worker(queue: TaskQueue, name: str, max_deliver: int, ran: list[str]) -> Worker:
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
        end=_ends_its_process,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "taken_by"),
    [
        pytest.param(_MAX_DELIVER, "next", id="with-a-delivery-to-spare-it-goes-to-another-replica"),
        pytest.param(1, "stopping", id="on-its-last-delivery-it-is-handled-where-it-arrived"),
    ],
)
async def test_a_task_a_pull_answers_as_its_worker_stops(fleet: _Fleet, maintenance_metrics: Measured, allowed: int, taken_by: str) -> None:
    """A pull in flight when a worker is asked to stop can still be answered. With a delivery to spare the task is given
    back at once and another replica runs it, rather than its pass starting under the drain; on the last delivery the
    worker allows it is handled where it arrived, since a hand-back would spend the consumer's last delivery and leave
    the task on the queue, undelivered, for good. Either way the task's wait is recorded once, at its first delivery, and
    the delivery the hand-back causes is counted as a redelivery, though no run failed."""
    ran: list[str] = []
    pulls = await fleet.pulls()
    stopping = _counting_worker(await fleet.lane("stopping"), "stopping", allowed, ran)
    lane = await fleet.lane("next")
    first = asyncio.create_task(stopping.run_tasks())
    fleet.on_stop.append(first)
    # Its first pull is out, and waits up to five seconds for a task.
    await pulls.next_after(0, 15)
    stopping.stop()
    assert await lane.enqueue(_task("late")) is True
    await asyncio.wait_for(first, timeout=15)
    fleet.on_stop.append(asyncio.create_task(_counting_worker(lane, "next", _MAX_DELIVER, ran).run_tasks()))

    async def settled() -> bool:
        return bool(ran) and not await lane.held("task", "table")

    assert await _until(settled, 30), f"the task was never run and settled: ran by {ran}"
    assert ran == [taken_by]
    counted = {name: maintenance_metrics.by(f"maintenance.tasks.{name}", "kind") for name in ("wait", "redelivered", "retried")}
    assert counted == {"wait": {"table": 1}, "redelivered": {"table": 1} if taken_by == "next" else {}, "retried": {}}


@pytest.mark.asyncio
async def test_a_connection_whose_bus_is_gone_still_closes(fleet: _Fleet) -> None:
    link = await fleet.link()
    queue = await TaskQueue.connect(link.url, fleet.bus.worker.creds_file.read_text(), name="orphaned")
    await fleet.links.pop().stop()

    closed: BaseException | None = None
    try:
        await asyncio.wait_for(queue.close(), timeout=30)
    except BaseException as exc:  # noqa: BLE001 — kept for the assertion below
        closed = exc

    assert closed is None, f"a connection whose bus is gone did not close: {closed!r}"


@pytest.mark.asyncio
async def test_an_idle_worker_stops_and_closes_its_connection_at_once(fleet: _Fleet) -> None:
    """A worker with nothing to do stops within one pull's wait, and its connection then closes at once: each loop ends
    the subscription it pulled on, so the connection's drain waits on none (measured on the estate before each did:
    an idle worker took 34 s to stop, the client's 30 s drain timeout, on every rollout)."""
    rig = await fleet.rig()
    pulled: set[str] = set()

    async def seen(msg: Msg) -> None:
        pulled.add(msg.subject)

    await rig.nc.subscribe("$JS.API.CONSUMER.MSG.NEXT.>", cb=seen)
    await rig.settle()

    async def handled(_body: bytes) -> int:
        return 0

    queue = await TaskQueue.connect(fleet.bus.broker.url, fleet.bus.worker.creds_file.read_text(), name="idle")
    idle = Worker(
        queue,
        run=_never_run,
        ready=lambda: True,
        heartbeat_seconds=_HEARTBEAT,
        ack_wait_seconds=_ACK_WAIT,
        max_deliver=_MAX_DELIVER,
        max_pass_seconds=600,
        retry_ended_seconds=3600,
        end=_ends_its_process,
        store_events=handled,
    )
    loops = [asyncio.create_task(loop()) for loop in (idle.run_tasks, idle.run_due, idle.run_store_events)]
    assert await _until(lambda: len(pulled) == len(loops), 15), f"not every loop pulled: {pulled}"

    began = asyncio.get_running_loop().time()
    await idle.drain(loops, 20)
    await queue.close()
    took = asyncio.get_running_loop().time() - began

    assert took < 10, f"an idle worker took {took:.1f} s to stop and close"


# --- what one claim keeps apart, and a claim that cannot be had -------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(Task(kind="audit"), Task(kind="scan"), id="the-audit-and-the-scan"),
        pytest.param(_task("shared"), _task("shared", kind="orphans"), id="a-tables-pass-and-its-orphan-check"),
    ],
)
@pytest.mark.asyncio
async def test_two_tasks_holding_one_claim_never_run_at_once(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch, first: Task, second: Task) -> None:
    """The audit and the scan each repair the estate from a drift report of their own, and a table's orphan check reads
    the files its pass's cleanup deletes, so each pair holds one claim: one waits out the other's while it runs, on two
    replicas, and runs once it is done."""
    # The give-back's delay is drawn at random beneath 15 s; drawn here at the bound, the second delivery comes after the
    # release below, where a short draw would spend the task's last delivery on the claim still held.
    monkeypatch.setattr(worker_queue, "redelivery_delay", lambda _delivered: 15.0)
    lane = await fleet.lane()
    runs = _Runs()
    release = runs.held[first.uuid] = asyncio.Event()
    for name in ("first-replica", "second-replica"):
        await fleet.start(name, runs)
    assert await lane.enqueue(first) is True
    assert await lane.enqueue(second) is True
    assert await _until(lambda: len(runs.ran) >= 1, 15), f"neither task ran: {runs.ran}"

    # The other's first delivery waits one ack wait for the claim, and comes back after its retry delay: nothing to poll
    # for while it must not run, and released before its second delivery, which a held claim would give back again.
    await asyncio.sleep(3 * _ACK_WAIT)
    beside = list(runs.ran)
    release.set()

    assert len(beside) == 1, f"two tasks holding one claim ran at once: {beside}"
    assert await _until(lambda: len(runs.ran) == 2, 60), f"the other never ran once the first was done: {runs.ran}"


class _OutlastedAudit(BaseModel):
    """What an audit that found the scan's claim held on every delivery left: whether it left the queue, which passes
    ran meanwhile, and which kinds the ended-tasks count was told of."""

    left: bool
    ran: list[tuple[str, str]]
    ended: list[str]


async def _an_audit_the_scan_outlasts(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch) -> _OutlastedAudit:
    runs = _Runs()
    release = runs.held[""] = asyncio.Event()
    ended: list[str] = []
    monkeypatch.setattr(worker_queue, "record_task_ended", lambda *, kind: ended.append(kind))
    lane = await fleet.lane()
    reader = await fleet.rig()
    for name in ("scan-replica", "audit-replica"):
        await fleet.start(name, runs)
    assert await lane.enqueue(Task(kind="scan")) is True
    assert await _until(lambda: len(runs.ran) == 1, 15), f"the scan never ran: {runs.ran}"
    assert await lane.enqueue(Task(kind="audit")) is True

    async def the_audit_left_the_queue() -> bool:
        answer = await reader.request(f"$JS.API.STREAM.MSG.GET.{TASKS_STREAM}", {"last_by_subj": Task(kind="audit").subject("task")})
        return answer is not None and "error" in answer

    left = await _until(the_audit_left_the_queue, 120)
    # Read before the scan is let go: a replica stopped mid-pass leaves its task to the other, which would run it.
    ran = list(runs.ran)
    release.set()
    return _OutlastedAudit(left=left, ran=ran, ended=ended)


@pytest.mark.asyncio
async def test_an_audit_the_scan_outlasts_leaves_the_queue_unrun(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit that finds the scan's claim held on every delivery leaves the queue on its last delivery without running;
    its next firing brings it back."""
    audit = await _an_audit_the_scan_outlasts(fleet, monkeypatch)

    assert (audit.left, audit.ran) == (True, [("scan-replica", "")]), f"the audit stayed queued, or ran beside the scan: {audit}"


@pytest.mark.asyncio
async def test_an_audit_the_scan_outlasts_is_not_counted_as_a_task_that_ended(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    """It failed at nothing, so the count the ended-tasks alert reads is not told of it."""
    audit = await _an_audit_the_scan_outlasts(fleet, monkeypatch)

    assert audit.ended == [], f"the audit was counted as ended: {audit}"


@pytest.mark.parametrize(
    ("act", "reason"),
    [
        pytest.param(_purge_the_claims, "maintenance_claim_lost", id="its-claim-gone"),
        pytest.param(_break_the_renewal, "maintenance_heartbeat_ended", id="its-heartbeat-ended-by-an-unexpected-error"),
    ],
)
@pytest.mark.asyncio
async def test_a_pass_whose_claim_is_gone_ends_its_replica(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch, act: Callable[[Client, pytest.MonkeyPatch], Awaitable[None]], reason: str
) -> None:
    """A pass renews its claim on its table with each heartbeat, and one that finds the claim no longer its own cannot
    tell whether another pass has taken the table: the replica ends itself rather than run beside it. So does one whose
    heartbeat ends by an error nothing expected, since the pass would run on with nothing vouching for it."""
    runs = _Runs()
    runs.held["claimed"] = asyncio.Event()
    lane = await fleet.lane()
    rig = await fleet.rig()
    replica = await fleet.start("claiming", runs)
    assert await lane.enqueue(_task("claimed")) is True
    assert await _until(lambda: runs.times("claimed") == 1, 15), f"the task never ran: {runs.ran}"

    await act(rig, monkeypatch)

    assert await _until(lambda: bool(replica.ended), 3 * _ACK_WAIT), "the replica ran on"
    assert replica.ended == [reason], f"the replica ended for another reason: {replica.ended}"


@pytest.mark.asyncio
async def test_a_worker_whose_claims_bucket_is_gone_ends_its_task_loop(fleet: _Fleet) -> None:
    """A worker binds the claims bucket before it takes a task; one the bus deleted since answers each claim with no
    stream, and the bucket's own lookup says it is gone for good. The task loop ends on the first claim, so the process
    ends and starts again, where a worker that kept asking would hold its task until the pass's hung bound ended it."""
    runs = _Runs()
    lane = await fleet.lane()
    rig = await fleet.rig()
    queue = await fleet.lane("unclaiming")
    await queue.bind_claims()
    deleted = await rig.request(f"$JS.API.STREAM.DELETE.KV_{CLAIMS_BUCKET}")
    assert deleted is not None and deleted.get("success") is True, f"the claims bucket was not deleted: {deleted}"
    replica = _Replica(queue, "unclaiming", runs, ready=lambda: True)
    fleet.replicas.append(replica)

    assert await lane.enqueue(_task("unclaimed")) is True

    assert await _until(lambda: replica.task_loop_failure() is not None, 3 * _ACK_WAIT), "the worker held on"
    assert (type(replica.task_loop_failure()), runs.times("unclaimed")) == (ClaimsBucketMissingError, 0), f"the worker held on: {replica.task_loop_failure()!r}"


@pytest.mark.asyncio
async def test_a_task_whose_claim_the_bus_does_not_answer_is_held_until_it_is_and_then_runs(fleet: _Fleet) -> None:
    """A claim the bus does not answer, its publish lost on the way as the network loses one, says nothing of whether
    another pass holds the table: the worker keeps the task and asks again, and runs it once the claim is answered,
    without spending a delivery on it. A bucket the bus holds no longer is an answer for good, which ends the worker's
    task loop instead (`test_a_worker_whose_claims_bucket_is_gone_ends_its_task_loop`)."""
    runs = _Runs()
    lane = await fleet.lane()
    rig = await fleet.rig()
    link = await fleet.link()
    link.drop(f"$KV.{CLAIMS_BUCKET}.>")
    replica = await fleet.start("waiting", runs, url=link.url)
    assert await lane.enqueue(_task("unclaimed")) is True
    # Longer than the worker's wait between asks, and well inside the first redelivery delay (15 s): nothing to poll for
    # while the task must not run.
    await asyncio.sleep(_ACK_WAIT)
    ran_unclaimed = list(runs.ran)

    link.flow()

    ran = await _until(lambda: runs.times("unclaimed") == 1, 30)
    consumer = await rig.request(f"$JS.API.CONSUMER.INFO.{TASKS_STREAM}.{TASK_CONSUMER}") or {}
    assert ran_unclaimed == [], f"the task ran with no claim on its table: {ran_unclaimed}"
    assert ran and replica.ended == [], f"the task did not run once its claim was answered: {runs.ran} {replica.ended}"
    assert consumer.get("num_redelivered") == 0, f"the unanswered claim spent {consumer.get('num_redelivered')} of the task's deliveries"


# --- bounds -----------------------------------------------------------------------------------------------------------


async def _a_task_run_past_twice_the_checks_bound(fleet: _Fleet, task: Task) -> list[str]:
    """``task`` run for four seconds, past twice the orphan check's one-second bound and well inside the pass bound, by a
    worker whose bounds are each kind's own (`maintenance.orphanCheck.maxSeconds` for a check,
    `maintenance.queue.maxPassSeconds` for the others): where the worker ended its process, in that time."""
    runs = _Runs()
    ended: list[str] = []
    runs.held[task.uuid] = asyncio.Event()
    queue = await fleet.lane("bounded")
    lane = await fleet.lane()

    def ends_its_process(event: str, **extra: object) -> NoReturn:
        ended.append(f"{event} {extra.get('subject')}")
        for loop in fleet.on_stop:
            loop.cancel()
        raise _Ended(event)

    replica = Worker(
        queue,
        run=runs.of("bounded"),
        ready=lambda: True,
        heartbeat_seconds=_HEARTBEAT,
        ack_wait_seconds=_ACK_WAIT,
        max_deliver=_MAX_DELIVER,
        max_pass_seconds=600,
        max_seconds_by_kind={"orphans": 1},
        retry_ended_seconds=3600,
        end=ends_its_process,
    )
    fleet.on_stop.append(asyncio.create_task(replica.run_tasks()))
    assert await lane.enqueue(task) is True
    assert await _until(lambda: runs.times(task.uuid) == 1, 15), f"the task never ran: {runs.ran}"
    # Past twice the check's bound, inside the pass's: an end, or none, is what these seconds show.
    await asyncio.sleep(4)
    return ended


@pytest.mark.asyncio
async def test_an_orphan_check_past_twice_its_own_bound_ends_its_replica(fleet: _Fleet) -> None:
    checking = _task("checking", kind="orphans")

    ended = await _a_task_run_past_twice_the_checks_bound(fleet, checking)

    assert ended == [f"maintenance_task_hung {checking.subject('task')}"], f"the check ran past twice its bound and its replica lived on: {ended}"


@pytest.mark.asyncio
async def test_a_pass_as_long_as_that_is_within_the_pass_bound(fleet: _Fleet) -> None:
    ended = await _a_task_run_past_twice_the_checks_bound(fleet, _task("passing"))

    assert ended == [], f"a pass within the pass bound ended its replica: {ended}"


# --- schedules --------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tables_first_check_is_held_once_and_a_held_one_keeps_its_time(fleet: _Fleet) -> None:
    """A table's pass holds its check only where none is held (`TaskQueue.schedule_first`): a second hold is refused and
    the first one's time stands, so a check's own interval is never replaced by a pass's."""
    lane = await fleet.lane()
    rig = await fleet.rig()
    check = _task("first-check", kind="orphans")
    soon, later = datetime.now(UTC) + timedelta(hours=1), datetime.now(UTC) + timedelta(days=1)

    holds = [await lane.schedule_first(check, soon), await lane.schedule_first(check, later)]

    held = _held_at(await rig.request(f"$JS.API.STREAM.MSG.GET.{SCHEDULES_STREAM}", {"last_by_subj": check.subject("schedule")}))
    assert (holds, "first-check" in await lane.held("schedule", "orphans")) == ([True, False], True)
    assert held is not None and abs((held - soon).total_seconds()) < 1, f"the held check's time was replaced: {held}"


@pytest.mark.asyncio
async def test_a_fired_schedule_that_is_no_task_is_ended_and_not_delivered_again(fleet: _Fleet) -> None:
    """What a schedule fires is moved onto the queue as the task it carries; one that carries none can never be moved, so
    it is ended, where giving it back would deliver it for ever (the consumer of fired schedules has no delivery bound)."""
    reader = await fleet.rig()
    await fleet.start("mover", _Runs())

    await reader.nc.publish("maintenance.due.table.garbage", b"no task")
    await reader.settle()

    async def the_schedules_hold_nothing() -> bool:
        state = (await reader.request(f"$JS.API.STREAM.INFO.{SCHEDULES_STREAM}") or {}).get("state", {})
        return state.get("messages") == 0 and state.get("last_seq", 0) >= 1

    assert await _until(the_schedules_hold_nothing, 20), "the fired schedule that is no task was still held, to be delivered again"


@pytest.mark.asyncio
async def test_a_fired_schedule_whose_move_the_bus_lost_is_moved_again(fleet: _Fleet) -> None:
    """A fired schedule whose task's publish never reached the bus, while the replica's pulls and settles still did, is
    given back and moved again once publishes flow: its task runs once, and not before."""
    runs = _Runs()
    link = await fleet.link()
    lane = await fleet.lane()
    replica = await fleet.start("mover", runs, url=link.url)
    link.drop("maintenance.task.>")
    assert await lane.follow_up(_task("moved"), datetime.now(UTC) + timedelta(seconds=1)) is True
    # The mover gives the fired schedule back once its publish goes unanswered, which the broker traces.
    assert await _until(lambda: "-NAK" in fleet.bus.logged_since(), 30), "the mover never gave back the schedule whose move was lost"
    before = list(runs.ran)

    link.flow()

    assert before == [], f"a task ran though its move's publish was lost: {before}"
    assert await _until(lambda: runs.times("moved") == 1, 45), f"the move was never made again once publishes flowed: {runs.ran}"
    assert replica.ended == [], f"the mover ended where a worker ends its process: {replica.ended}"


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [pytest.param(False, id="queued-directly"), pytest.param(True, id="held-as-a-follow-up")])
async def test_a_tasks_run_is_a_span_of_its_own_linked_to_the_trace_that_queued_it(fleet: _Fleet, monkeypatch: pytest.MonkeyPatch, held: bool) -> None:
    """A task's run is a consumer span that starts a trace of its own and links to the span the task was queued under,
    whether it was put on the queue directly or held as a schedule the worker moved there when it fired: the context
    rides the message's headers across both hops. A link and not a parent, since each pass holds its table's next pass
    from inside its own span, and a parent would chain every pass of a table into one trace."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(worker_queue, "tracer", provider.get_tracer(worker_queue.__name__), raising=False)
    runs = _Runs()
    lane = await fleet.lane()
    await fleet.start("runner", runs)
    with provider.get_tracer("a-caller").start_as_current_span("queues-a-task") as caller:
        queued_under = caller.get_span_context()
        if held:
            assert await lane.follow_up(_task("traced"), datetime.now(UTC) + timedelta(seconds=1)) is True
        else:
            assert await lane.enqueue(_task("traced")) is True

    assert await _until(lambda: runs.times("traced") == 1 and len(_task_spans(exporter)) == 1, 45), (
        f"the task did not run under a span: ran {runs.ran}, spans {[span.name for span in exporter.get_finished_spans()]}"
    )
    (span,) = _task_spans(exporter)
    assert (span.kind, span.parent, [link.context.span_id for link in span.links], span.context.trace_id == queued_under.trace_id) == (
        SpanKind.CONSUMER,
        None,
        [queued_under.span_id],
        False,
    )


#: The name of the span a table task's run is.
_TASK_SPAN = "process maintenance.task.table"


def _task_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in exporter.get_finished_spans() if span.name == _TASK_SPAN]


@pytest.mark.asyncio
async def test_a_fired_schedule_the_queue_refuses_is_counted_and_given_back(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    """A fired schedule the queue does not take (its stream is gone, as on a bus whose stream Job has not run again yet)
    is counted on `maintenance.schedules.not_queued` and given back, and is moved once the queue takes it again."""
    rig = await fleet.rig()
    dropped = await rig.request(f"$JS.API.STREAM.DELETE.{TASKS_STREAM}")
    assert dropped is not None and dropped.get("success") is True, f"the task stream was not removed: {dropped}"
    queue = await fleet.lane("mover")
    mover = Worker(
        queue,
        run=_never_run,
        ready=lambda: True,
        heartbeat_seconds=_HEARTBEAT,
        ack_wait_seconds=_ACK_WAIT,
        max_deliver=_MAX_DELIVER,
        max_pass_seconds=600,
        retry_ended_seconds=3600,
    )
    fleet.on_stop.append(asyncio.create_task(mover.run_due()))
    await queue.schedule_at(_task("refused"), datetime.now(UTC))
    refused = await _until(lambda: maintenance_metrics.of("maintenance.schedules.not_queued") == 1, 20)

    await asyncio.to_thread(fleet.bus.declare)

    async def queued() -> bool:
        return "refused" in await queue.held("task", "table")

    assert (refused, await _until(queued, 60)) == (True, True), "the refused schedule was not counted, or not moved once the queue took it again"


# --- the sampler and the task timings ---------------------------------------------------------------------------------

#: How old the newest of the sampled messages is when the sampler reads them, in seconds.
_AGED = 5.0


class _Backlog:
    """A task running and one queued behind it on the queue, and, when asked, a store event delivered twice and unsettled,
    with when each was put."""

    def __init__(self, fleet: _Fleet) -> None:
        self.fleet = fleet
        self.runs = _Runs()
        self.release = self.runs.held["running"] = asyncio.Event()
        self.runs.held["waiting"] = asyncio.Event()
        self.clock = asyncio.get_running_loop().time
        self.queued_at = self.started_at = self.published_at = 0.0

    async def build(self, *, with_a_store_event: bool) -> TaskQueue:
        lane = await self.fleet.lane("sampler")
        await self.fleet.start("running", self.runs)
        self.queued_at = self.clock()
        assert await lane.enqueue(_task("running")) is True
        assert await _until(lambda: self.runs.times("running") == 1, 15), f"the first task never ran: {self.runs.ran}"
        self.started_at = self.clock()
        assert await lane.enqueue(_task("waiting")) is True
        self.published_at = self.clock()
        if with_a_store_event:
            rig = await self.fleet.rig()
            store = await Client.connect(self.fleet.bus.broker.url, self.fleet.bus.mint.credentials[_STORE], _STORE)
            self.fleet.clients.append(store)
            await store.js.publish("store.events", _write_event("t/data/a.lance"), timeout=10)
            events = await rig.js.pull_subscribe_bind(durable="maintenance-store-events", stream=STORE_EVENTS_STREAM)
            await (await events.fetch(1, timeout=5))[0].nak()
            await events.fetch(1, timeout=5)
            # An age is read over elapsed time: this ages the oldest message well past the tolerance the reading is held
            # to, so an age read as 0 is told apart.
            await asyncio.sleep(_AGED - (self.clock() - self.published_at))
        return lane


@pytest.mark.asyncio
async def test_the_sampler_reads_each_streams_depth_and_age_and_each_durables_backlog_as_the_bus_holds_them(
    fleet: _Fleet, maintenance_metrics: Measured
) -> None:
    """The worker's sampler (`maintenance.services.queue.sample_queue`), as `maintenance-worker` under the grants the
    render issues, reads each stream's depth and its oldest message's age, and each durable's messages not yet delivered,
    deliveries awaiting an ack and redeliveries: a task running and one queued behind it, and a store event delivered
    twice and unsettled."""
    backlog = _Backlog(fleet)
    lane = await backlog.build(with_a_store_event=True)

    unread = await sample_queue(lane)

    sampled_at = backlog.clock()
    oldest = maintenance_metrics.by("maintenance.queue.oldest", "stream")
    assert unread == 0
    assert maintenance_metrics.by("maintenance.queue.messages", "stream") == {TASKS_STREAM: 2, SCHEDULES_STREAM: 0, STORE_EVENTS_STREAM: 1}
    assert set(oldest) == {TASKS_STREAM, STORE_EVENTS_STREAM}
    assert abs(oldest[TASKS_STREAM] - (sampled_at - backlog.queued_at)) < 1.5, f"the oldest task's age read {oldest[TASKS_STREAM]:.1f} s"
    assert abs(oldest[STORE_EVENTS_STREAM] - (sampled_at - backlog.published_at)) < 1.5, f"the oldest event's age read {oldest[STORE_EVENTS_STREAM]:.1f} s"
    assert {name: maintenance_metrics.by(f"maintenance.queue.{name}", "consumer") for name in ("waiting", "ack_pending", "redelivered")} == {
        "waiting": {"maintenance-worker": 1, "maintenance-due": 0, "maintenance-store-events": 0},
        "ack_pending": {"maintenance-worker": 1, "maintenance-due": 0, "maintenance-store-events": 1},
        "redelivered": {"maintenance-worker": 0, "maintenance-due": 0, "maintenance-store-events": 1},
    }


@pytest.mark.asyncio
async def test_a_stream_the_sampler_cannot_read_keeps_its_gauges_at_their_last_values(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    backlog = _Backlog(fleet)
    lane = await backlog.build(with_a_store_event=True)
    assert await sample_queue(lane) == 0
    last = (
        maintenance_metrics.of("maintenance.queue.messages", stream=STORE_EVENTS_STREAM),
        maintenance_metrics.of("maintenance.queue.oldest", stream=STORE_EVENTS_STREAM),
        maintenance_metrics.of("maintenance.queue.ack_pending", consumer="maintenance-store-events"),
    )
    rig = await fleet.rig()
    dropped = await rig.request(f"$JS.API.STREAM.DELETE.{STORE_EVENTS_STREAM}")
    assert dropped is not None and dropped.get("success") is True, f"the store events' stream was not removed: {dropped}"

    unread = await sample_queue(lane)

    kept = (
        maintenance_metrics.of("maintenance.queue.messages", stream=STORE_EVENTS_STREAM),
        maintenance_metrics.of("maintenance.queue.oldest", stream=STORE_EVENTS_STREAM),
        maintenance_metrics.of("maintenance.queue.ack_pending", consumer="maintenance-store-events"),
    )
    assert (unread, kept) == (2, last)


@pytest.mark.asyncio
async def test_a_task_is_timed_from_its_claim_to_its_acknowledgement(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    backlog = _Backlog(fleet)
    await backlog.build(with_a_store_event=False)
    released_at = backlog.clock()

    backlog.release.set()

    timed = await _until(lambda: maintenance_metrics.of("maintenance.tasks.duration", kind="table") == 1, 15)
    acked_by = backlog.clock()
    total = maintenance_metrics.total("maintenance.tasks.duration", kind="table")
    assert timed and total is not None and released_at - backlog.started_at <= total <= acked_by - backlog.queued_at, f"the task ran {total} s by its count"


@pytest.mark.asyncio
async def test_a_tasks_wait_is_timed_from_its_queuing_to_its_first_delivery(fleet: _Fleet, maintenance_metrics: Measured) -> None:
    """The first task is delivered as it is queued; the second waits behind it from its queuing, before `published_at`,
    until the first is released."""
    backlog = _Backlog(fleet)
    await backlog.build(with_a_store_event=False)
    released_at = backlog.clock()

    backlog.release.set()

    waited = await _until(lambda: maintenance_metrics.of("maintenance.tasks.wait", kind="table") == 2, 15)
    delivered_by = backlog.clock()
    waits = maintenance_metrics.total("maintenance.tasks.wait", kind="table")
    assert waited and waits is not None and released_at - backlog.published_at <= waits <= delivered_by - backlog.queued_at, (
        f"the two tasks waited {waits} s by their count"
    )
