"""Something on the bus holds every live table between its passes (ADR 0232, invariant `HeldUnlessBackstopped`).

Each test is one assumption of the model of maintenance's work queue (`specs/maintenance/WorkQueue.tla`, A5 and A6), the
shortest trace of the config that removes it played on the running code: the worker (`maintenance.services.queue.Worker`
over `TaskQueue`), its task runner (`maintenance.services.tasks.run_task`) and its commit lane
(`maintenance.services.arrival.Arrivals.hold_task`), on the pinned nats-server (`scripts/nats_server.sh`, the release the
estate's bus runs) with JetStream, its streams, durable consumers and claims bucket declared as the chart's stream Job
declares them (`chart/templates/nats-stream-job.yaml` 151-169, `lance.busConsumers` in `chart/templates/_helpers.tpl`
408-418), at one replica. One step is held at the moment the trace names, while every other runs as it would; the harm is
read as the hourly audit reads it (`audit._queue_what_holds_nothing`: `TaskQueue.held` over the table's schedules, held
or fired, and its task), once the step has settled.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import nats
import pytest
from nats.aio.client import Client
from nats.aio.msg import Msg

from lakehouse import location_claims, trash
from lakehouse.base_refs import normalise
from lakehouse.cloud_events import lineage_envelope
from lakehouse.control_emit import NoopControlEmitter
from lakehouse.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import tasks
from maintenance.services.arrival import Arrivals
from maintenance.services.expiry import DatasetResult
from maintenance.services.naming import TableIds
from maintenance.services.plan import PlanContext
from maintenance.services.queue import DUE_CONSUMER, INBOX_PREFIX, SCHEDULES_STREAM, TASKS_STREAM, Task, TaskQueue, Worker
from maintenance.services.table_pass import PassOutcome, PassReadings, TableTask, next_due
from maintenance.services.tasks import run_task


ROOT = Path(__file__).resolve().parents[2]
#: The streams and the claims bucket as the chart's stream Job writes them (`nats-stream-job.yaml` 151, 153 and 169),
#: at one replica and the chart's default ack wait of 60 s (`maintenance.queue.ackWaitSeconds`).
_STREAMS: tuple[dict[str, Any], ...] = (
    {
        "name": "MAINTENANCE_TASKS",
        "subjects": ["maintenance.task.>"],
        "retention": "workqueue",
        "max_consumers": -1,
        "max_msgs": -1,
        "max_bytes": -1,
        "max_msgs_per_subject": 1,
        "max_msg_size": -1,
        "max_age": 86400000000000,
        "storage": "file",
        "discard": "new",
        "discard_new_per_subject": True,
        "num_replicas": 1,
        "duplicate_window": 120000000000,
        "deny_delete": True,
        "deny_purge": True,
        "allow_rollup_hdrs": False,
        "allow_direct": True,
    },
    {
        "name": "MAINTENANCE_SCHEDULES",
        "subjects": ["maintenance.schedule.>", "maintenance.due.>"],
        "retention": "workqueue",
        "max_consumers": -1,
        "max_msgs": -1,
        "max_bytes": -1,
        "max_msgs_per_subject": -1,
        "max_msg_size": -1,
        "max_age": 0,
        "storage": "file",
        "discard": "old",
        "num_replicas": 1,
        "duplicate_window": 120000000000,
        "allow_msg_schedules": True,
        "allow_rollup_hdrs": True,
        "allow_direct": True,
    },
    {
        "name": "KV_MAINTENANCE_CLAIMS",
        "subjects": ["$KV.MAINTENANCE_CLAIMS.>"],
        "retention": "limits",
        "max_consumers": -1,
        "max_msgs": -1,
        "max_bytes": -1,
        "max_msgs_per_subject": 1,
        "max_msg_size": -1,
        "max_age": 60000000000,
        "storage": "file",
        "discard": "new",
        "num_replicas": 1,
        "duplicate_window": 60000000000,
        "deny_delete": True,
        "deny_purge": False,
        "allow_rollup_hdrs": False,
        "allow_direct": True,
    },
)
#: The two durables the worker pulls, as `lance.busConsumers` renders them at the chart's defaults (ack wait 60 s,
#: `maxDeliver` 10 and the one delivery past it the consumer allows; the fired schedules' consumer redelivers for ever).
_CONSUMERS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "MAINTENANCE_TASKS",
        {
            "durable_name": "maintenance-worker",
            "ack_policy": "explicit",
            "ack_wait": 60000000000,
            "max_deliver": 11,
            "filter_subject": "maintenance.task.>",
            "deliver_policy": "all",
            "replay_policy": "instant",
            "max_ack_pending": 1000,
            "max_waiting": 512,
        },
    ),
    (
        "MAINTENANCE_SCHEDULES",
        {
            "durable_name": "maintenance-due",
            "ack_policy": "explicit",
            "ack_wait": 30000000000,
            "max_deliver": -1,
            "filter_subject": "maintenance.due.>",
            "deliver_policy": "all",
            "replay_policy": "instant",
            "max_ack_pending": 1000,
            "max_waiting": 512,
        },
    ),
)
#: How long the test waits for a step the trace names before it judges what the bus holds.
_STEP_SECONDS = 15.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    """The pinned nats-server with JetStream, on a free port and a store of the test's own. `scripts/nats_server.sh` fetches
    it into `.localbin/` when it is not there (`make test` does so first), and a lane that can neither find nor fetch it
    fails here, naming why: a broker these tests need is no reason to skip them."""
    fetched = subprocess.run(["bash", str(ROOT / "scripts" / "nats_server.sh")], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if fetched.returncode != 0:
        pytest.fail(f"scripts/nats_server.sh could not fetch the pinned nats-server (scripts/toolchain.sha256): {fetched.stderr.strip()}")
    server = fetched.stdout.strip()
    port = _free_port()
    process = subprocess.Popen(
        [server, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(tmp_path / "jetstream")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )  # noqa: S603
    try:
        until = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                assert time.monotonic() < until, "the nats-server did not listen"
                time.sleep(0.05)
        yield f"nats://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


async def _api(client: Client, subject: str, body: Mapping[str, Any]) -> dict[str, Any]:
    answer = json.loads((await client.request(subject, json.dumps(body).encode(), timeout=10)).data)
    assert "error" not in answer, f"{subject}: {answer['error']}"
    return answer


@asynccontextmanager
async def _queue_on(url: str) -> AsyncGenerator[TaskQueue]:
    """The worker's queue on a connection to ``url`` under the worker's inbox prefix, the streams and consumers declared
    first. The bus session's renewal is the `nats-auth` lane's, so nothing renews here."""
    client = await nats.connect(servers=[url], inbox_prefix=INBOX_PREFIX)
    for stream in _STREAMS:
        await _api(client, f"$JS.API.STREAM.CREATE.{stream['name']}", stream)
    for stream_name, consumer in _CONSUMERS:
        await _api(client, f"$JS.API.CONSUMER.CREATE.{stream_name}.{consumer['durable_name']}", {"stream_name": stream_name, "config": consumer})
    queue = TaskQueue(client, asyncio.Event(), asyncio.create_task(_renews_nothing()))
    try:
        yield queue
    finally:
        await queue.close()


async def _renews_nothing() -> None:
    await asyncio.Event().wait()


def _never_ends(event: str, **extra: object) -> NoReturn:
    raise AssertionError(f"the worker ended its process ({event}, {extra}): no fault is in the trace")


@asynccontextmanager
async def _worker(queue: TaskQueue, settings: MaintenanceSettings, run: Callable[[Task], Awaitable[None]]) -> AsyncGenerator[Worker]:
    """One replica's worker at the settings' timings, pulling its tasks and moving its fired schedules until the block
    ends."""
    worker = Worker(
        queue,
        run=run,
        ready=lambda: True,
        heartbeat_seconds=settings.task_heartbeat_seconds,
        ack_wait_seconds=settings.task_ack_wait_seconds,
        max_deliver=settings.task_max_deliver,
        max_pass_seconds=settings.max_pass_seconds,
        retry_ended_seconds=settings.recheck_hours * 3600,
        end=_never_ends,
    )
    loops = [asyncio.create_task(worker.run_tasks()), asyncio.create_task(worker.run_due())]
    try:
        yield worker
    finally:
        await worker.drain(loops, 7.0)


async def _until(done: Callable[[], Awaitable[bool]], seconds: float = _STEP_SECONDS) -> bool:
    until = time.monotonic() + seconds
    while not await done():
        if time.monotonic() > until:
            return False
        await asyncio.sleep(0.05)
    return True


async def _quiet(queue: TaskQueue) -> bool:
    """Whether the bus has nothing left to do: no task queued or in flight, and no fired schedule waiting to be moved."""
    tasks_held = (await queue.stream_state(TASKS_STREAM)).messages
    due = await queue.consumer_state(SCHEDULES_STREAM, DUE_CONSUMER)
    return tasks_held == 0 and due.num_pending == 0 and due.num_ack_pending == 0


async def _holds(queue: TaskQueue, uuid: str) -> bool:
    """Whether the bus holds the table as the hourly audit reads it: a schedule of its own, held or fired, or a task on the
    queue."""
    return uuid in await queue.held("schedule", "table") | await queue.held("task", "table")


def _settings(tmp_path: Path) -> MaintenanceSettings:
    control = tmp_path / "control"
    control.mkdir(exist_ok=True)
    return MaintenanceSettings(MAINTENANCE_CONTROL_ROOT=str(control), MAINTENANCE_POLICY_ROOT=str(control))


@pytest.mark.asyncio
async def test_a_pass_that_holds_a_time_already_past_leaves_its_table_held(bus: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A table's pass counts its next time from its start (`table_pass.run_table_pass`, `next_due`), so a pass longer than
    that time holds one already past (`tasks.run_task`), which fires at once, while the pass still runs.

    The pass is the real `next_due` over a pass that began `retry_seconds` and half a minute ago and could not decide one
    dataset, which holds the next try `retry_seconds` from the start: a pass of 15.5 min, inside the half-hour bound,
    whose store did not answer for one dataset. The held step is the pass's ack (`Worker._handle`): it is sent once the
    fired schedule has met the pass's own task in flight (`Worker._move`, `TaskQueue.enqueue` refused) and the move has
    settled the fired schedule. What the bus holds once the pass has acked is what the hourly audit reads."""
    settings = _settings(tmp_path)
    started = datetime.now(UTC) - timedelta(seconds=settings.retry_seconds + 30)

    def long_pass(task: TableTask, settings: MaintenanceSettings) -> PassOutcome:
        readings = PassReadings(table_ids=TableIds(), context=PlanContext(policies=[], now=started, older_than=timedelta(days=settings.older_than_days)))
        undecided = DatasetResult(uri=task.location, error="the store did not answer")
        return PassOutcome(results=[undecided], next_due=next_due([undecided], readings=readings, settings=settings))

    monkeypatch.setattr(tasks, "run_table_pass", long_pass)
    table = Task(kind="table", uuid=str(uuid4()), location=str(tmp_path / "table"))
    async with _queue_on(bus) as queue:
        covered, settled = asyncio.Event(), asyncio.Event()
        enqueue, told, move = queue.enqueue, queue.told, Worker._move

        async def moved(task: Task, *, carried: Mapping[str, str] | None = None) -> bool:
            queued = await enqueue(task, carried=carried)
            if not queued and task.uuid == table.uuid:
                covered.set()
            return queued

        async def settles(worker: Worker, msg: Msg) -> None:
            await move(worker, msg)
            if msg.subject == table.subject("due") and covered.is_set():
                settled.set()

        async def acked_after_the_move(msg: Msg, said: bytes, *, within: float) -> bool:
            if said == Msg.Ack.Ack and msg.subject == table.subject("task"):
                await asyncio.wait_for(covered.wait(), _STEP_SECONDS)
                await asyncio.wait_for(settled.wait(), _STEP_SECONDS)
            return await told(msg, said, within=within)

        monkeypatch.setattr(queue, "enqueue", moved)
        monkeypatch.setattr(queue, "told", acked_after_the_move)
        monkeypatch.setattr(Worker, "_move", settles)
        async with _worker(queue, settings, partial(run_task, settings=settings, queue=queue, emitter=NoopEmitter(), control=NoopControlEmitter())):
            assert await queue.enqueue(table), "the table's first task was not queued"
            assert await _until(lambda: _acked(queue, settled)), "the pass never acked"
        assert await _holds(queue, table.uuid), (
            "the pass held its table's next time already past; it fired, the move found the pass's own task in flight and "
            "acked it as covered, the pass acked, and nothing on the bus holds the live table until the hourly audit"
        )


async def _acked(queue: TaskQueue, settled: asyncio.Event) -> bool:
    """Whether the move has settled the fired schedule and the pass's task has left the queue, acknowledged."""
    return settled.is_set() and (await queue.stream_state(TASKS_STREAM)).messages == 0


def _announcement(operation: str, *, uuid: str, location: str) -> bytes:
    """A delivery of the commit lane: the catalog's announcement of ``operation`` on the table, as
    `catalog.core.lineage_emit` builds one (the table, its uuid and its location) in its envelope on the lineage topic."""
    output = with_dataset_uuids(
        {"name": "lance_catalog$db$t", "namespace": "rask", "facets": {"dataSource": {"name": location, "uri": location}}},
        DatasetUuids(table_uuid=uuid),
        producer="https://example.test/catalog",
    )
    event = {"eventType": "COMPLETE", "run": {"runId": str(uuid4()), "facets": {"rask_lance": {"operation": operation}}}, "outputs": [output]}
    return json.dumps(lineage_envelope(event).as_wire()).encode()


#: The commit delay the second test holds follow-ups at, where the chart's is five minutes: it only brings their fires
#: within the test. The undrop announces the table `_UNDROP_AFTER` later than the write, so the write's follow-up comes
#: due between the announcement and the clear while the announcement's own does not: `@at` holds to the second
#: (`queue.at_schedule`), and a follow-up's pass starts 3 to 13 ms after its time (ADR 0232, measured on the pinned
#: server), so the two fires stand at least `_UNDROP_AFTER` less a second apart.
_DELAY_SECONDS = 6
_UNDROP_AFTER = 3.0


@pytest.mark.asyncio
async def test_an_undropped_table_is_held_when_an_older_follow_up_fires_before_the_clear(bus: str, tmp_path: Path) -> None:
    """A table is written (its follow-up held `commitPassDelaySeconds` out, `Arrivals._held`), dropped (its trash record
    filed) and undropped: the undrop announces the table (`undrop_table`'s `filing.send(restored)` in the catalog's
    `tables.py`) and then clears the record. The write's follow-up comes due between the two; covered by it, the
    announcement would leave the table to a pass that reads it in the trash and holds no next time
    (`table_pass.run_table_pass`, `tasks.run_task`).

    The held step is the clear: it lands once the write's follow-up's time has passed and the bus is quiet, when a pass
    of that follow-up would have read the table in the trash. The door's two writes are made here in the door's order,
    the announcement through the lane's own handling of its delivery and the clear through `trash.clear`, on the claim and
    the trash record the catalog keeps. What the bus holds once the next pass has run is what the hourly audit reads."""
    settings = _settings(tmp_path).model_copy(update={"commit_pass_delay_seconds": _DELAY_SECONDS})
    control, options = settings.resolved_control_root, StorageOptions()
    uuid, location = str(uuid4()), str(tmp_path / "table")
    claims = location_claims.ClaimStore(control_root=control, storage_options={})
    location_claims.take(claims, normalise(location), "lance_catalog$db$t", ["lance_catalog", "db", "t"], is_gone=lambda _claim: False, uuid=uuid)
    async with _queue_on(bus) as queue:
        passed = asyncio.Event()

        async def run(task: Task) -> None:
            await run_task(task, settings=settings, queue=queue, emitter=NoopEmitter(), control=NoopControlEmitter())
            if task.uuid == uuid:
                passed.set()

        lane = Arrivals(settings, queue)
        async with _worker(queue, settings, run):
            written = time.monotonic()
            assert await lane.hold_task(_announcement("insert", uuid=uuid, location=location)) == "held", "the write held no follow-up"
            trash.put(control, options, trash.make_record("lance_catalog$db$t", uuid=uuid, location=location, dropped_by="user:karin", grace_days=7))
            await asyncio.sleep(_UNDROP_AFTER)
            await lane.hold_task(_announcement("register_table", uuid=uuid, location=location))
            await asyncio.sleep(max(written + _DELAY_SECONDS + 1.5 - time.monotonic(), 0.0))
            assert await _until(lambda: _quiet(queue)), "the bus did not settle before the clear"
            trash.clear(control, options, uuid)
            await asyncio.wait_for(passed.wait(), _STEP_SECONDS)
            await _until(lambda: _quiet(queue))
        assert not trash.keeps(control, options, uuid), "the undrop's clear did not land"
        assert await _holds(queue, uuid), (
            "the undrop's announcement was covered by the write's follow-up, which fired before the clear and whose pass read "
            "the table in the trash; once the record was cleared nothing on the bus holds the live table until the hourly audit"
        )
