"""A worker's queue asks for its bus session again before the bus ends it, on the client that holds it, and that client
is back in a round trip. The readiness read of the queue answers for the connection that came back: a read that fell in
the moment between the two would report the bus away at every renewal, when nothing was."""

import asyncio
from unittest.mock import create_autospec

import pytest
from nats.aio.client import Client

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
