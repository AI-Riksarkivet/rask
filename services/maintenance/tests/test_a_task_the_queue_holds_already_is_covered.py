"""[[LH-494]] A task or a follow-up the queue holds already covers the one being put, so putting it answers ``False``, never
a refusal. JetStream refuses the guarded publish (``Nats-Expected-Last-Subject-Sequence: 0``) with "wrong last sequence"
under two codes: 10071 on one nats-server 2.15.2, and 10164 on the dev estate's three-node cluster, whose streams are R3
(measured 2026-11-09, where a store event's follow-up was retried as a refusal). The bus lane runs one server, so the
publish here is refused as each answers it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from nats.aio.client import Client
from nats.js.api import PubAck
from nats.js.client import JetStreamContext
from nats.js.errors import APIError

from maintenance.services.queue import Task, TaskQueue


class _Refusing:
    """A JetStream context that refuses every publish as a stream refuses one whose subject holds a message."""

    def __init__(self, err_code: int) -> None:
        self._err_code = err_code

    async def publish(
        self, subject: str, payload: bytes = b"", timeout: float | None = None, stream: str | None = None, headers: dict[str, Any] | None = None
    ) -> PubAck:
        raise APIError(code=400, err_code=self._err_code, description="wrong last sequence")


class _Client:
    """A bus client whose JetStream context is ``js``."""

    def __init__(self, js: _Refusing) -> None:
        self._js = js

    def jetstream(self) -> JetStreamContext:
        return cast(JetStreamContext, self._js)


async def _a_task(queue: TaskQueue, task: Task) -> bool:
    return await queue.enqueue(task)


async def _a_follow_up(queue: TaskQueue, task: Task) -> bool:
    return await queue.follow_up(task, datetime.now(UTC) + timedelta(minutes=5))


@pytest.mark.asyncio
@pytest.mark.parametrize("err_code", [pytest.param(10071, id="one-server"), pytest.param(10164, id="a-cluster")])
@pytest.mark.parametrize("put", [pytest.param(_a_task, id="a-task"), pytest.param(_a_follow_up, id="a-follow-up")])
async def test_a_task_the_queue_holds_already_is_covered(put: Callable[[TaskQueue, Task], Awaitable[bool]], err_code: int) -> None:
    queue = TaskQueue(cast(Client, _Client(_Refusing(err_code))))

    held = await put(queue, Task(kind="table", uuid="t", location="s3://bucket/t"))

    assert held is False
