"""A worker's queue asks for its bus session again before the bus ends it, on the client that holds it, and that client
is back in a round trip. The readiness read of the queue answers for the connection that came back: a read that fell in
the moment between the two would report the bus away at every renewal, when nothing was."""

import asyncio
from typing import NoReturn
from unittest.mock import AsyncMock, create_autospec

import pytest
from nats.aio.client import Client

from lakehouse.events.bus import BusSettings
from lakehouse.events.control_emit import NoopControlEmitter
from lakehouse.process.supervision import Supervisor
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import tasks
from maintenance.services.queue import QueueUnavailableError, TaskQueue


@pytest.mark.parametrize(
    ("back_after", "answers"),
    [
        pytest.param(0.1, True, id="the-connection-comes-back"),
        pytest.param(None, False, id="the-connection-stays-away"),
    ],
)
@pytest.mark.asyncio
async def test_a_readiness_read_waits_for_a_connection_that_is_reconnecting(monkeypatch: pytest.MonkeyPatch, back_after: float | None, answers: bool) -> None:
    monkeypatch.setattr("maintenance.services.queue._RECONNECTED_WITHIN_SECONDS", 0.5)
    client = create_autospec(Client, instance=True)
    client.is_connected, client.is_reconnecting = False, True

    def back() -> None:
        client.is_connected, client.is_reconnecting = True, False

    if back_after is not None:
        asyncio.get_running_loop().call_later(back_after, back)
    queue = TaskQueue(client, asyncio.Event(), asyncio.create_task(asyncio.sleep(0)))

    if answers:
        await queue.answering()
    else:
        with pytest.raises(QueueUnavailableError, match="not up"):
            await queue.answering()


class _Ended(Exception):  # noqa: N818 — what stands in for the process's end, not an error
    pass


@pytest.mark.asyncio
async def test_a_renewal_that_fails_ends_the_process_as_a_kept_loop_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection nothing renews is closed by the bus at the session's end, so the loop that renews it is supervised."""
    monkeypatch.setattr("lakehouse.events.bus._RENEW_MARGIN_SECONDS", (300.0, 300.0))
    client = create_autospec(Client, instance=True)
    client.force_reconnect = AsyncMock(side_effect=RuntimeError("the socket is gone"))
    monkeypatch.setattr("maintenance.services.queue.nats.connect", AsyncMock(return_value=client))
    ended: list[tuple[str, object, str]] = []

    def end(event: str, **extra: object) -> NoReturn:
        ended.append((event, extra.get("loop"), str(extra.get("cause"))))
        raise _Ended(event)

    async with Supervisor(end=end) as loops:
        queue = await TaskQueue.connect("nats://bus:4222", BusSettings(session_seconds=300), name="maintenance-worker", loops=loops)
        async with asyncio.timeout(5):
            while not ended and not client.force_reconnect.await_count:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0)  # the turn in which a task that has ended tells its supervisor
        await queue.close()

    assert ended == [("background_work_ended", "maintenance-bus-session", "the socket is gone")]


@pytest.mark.asyncio
async def test_a_renewal_that_fails_while_the_worker_drains_ends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker runs on a queue whose bus answers nothing: its pulls end empty once the renewal has failed, so the
    renewal's failure falls inside the drain, which waits for the loops that pull."""
    monkeypatch.setattr("lakehouse.events.bus._RENEW_MARGIN_SECONDS", (float("inf"), float("inf")))
    draining, failed = asyncio.Event(), asyncio.Event()

    async def _gone_once_the_worker_drains() -> None:
        await draining.wait()
        failed.set()
        raise RuntimeError("the socket is gone")

    async def _nothing_pulled(_queue: TaskQueue, _stream: str, _consumer: str, *, wait: float, answered_within: float) -> None:
        await failed.wait()

    async def _unanswered(_queue: TaskQueue, *_asked: str) -> NoReturn:
        raise QueueUnavailableError("the bus answers nothing")

    client = create_autospec(Client, instance=True)
    client.force_reconnect = AsyncMock(side_effect=_gone_once_the_worker_drains)
    monkeypatch.setattr("maintenance.services.queue.nats.connect", AsyncMock(return_value=client))
    monkeypatch.setattr(TaskQueue, "bind_claims", AsyncMock())
    monkeypatch.setattr(TaskQueue, "schedule_every", AsyncMock())
    monkeypatch.setattr(TaskQueue, "pulled", _nothing_pulled)
    monkeypatch.setattr(TaskQueue, "stream_state", _unanswered)
    monkeypatch.setattr(TaskQueue, "consumer_state", _unanswered)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"})
    ended: list[tuple[str, object]] = []

    def end(event: str, **extra: object) -> NoReturn:
        ended.append((event, extra.get("loop")))
        raise _Ended(event)

    async with (
        asyncio.timeout(10),
        tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter(), secrets=None, end=end),
    ):
        draining.set()

    assert (failed.is_set(), ended) == (True, [])
