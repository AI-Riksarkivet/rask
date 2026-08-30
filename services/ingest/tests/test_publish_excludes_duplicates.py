"""`publish_units` reports the units the stream actually ACCEPTED, not every unit it sent.

Each `js.publish` returns a `PubAck` whose `.duplicate` flag says whether the stream's dedupe window
rejected the message as one it already holds. The loop discarded the ack and counted `published += 1`
for every task, so a replayed chunk — whose whole point is that JetStream dedupes it — reported its
units as freshly accepted. The count is what the workflow reasons about, so a server-side dedupe read
as an acceptance overstates what landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ingest.queue import UnitTask, WorkQueue, _dedupe_id


if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext


class _Ack:
    def __init__(self, *, duplicate: bool) -> None:
        self.duplicate = duplicate


class _FakeJS:
    def __init__(self, duplicate_ids: set[str]) -> None:
        self._duplicate_ids = duplicate_ids
        self.published: list[str] = []

    async def publish(self, _subject: str, _payload: bytes, headers: dict[str, str] | None = None) -> _Ack:
        msg_id = (headers or {})["Nats-Msg-Id"]
        self.published.append(msg_id)
        return _Ack(duplicate=msg_id in self._duplicate_ids)


@pytest.mark.asyncio
async def test_duplicates_are_published_but_not_counted() -> None:
    tasks = [UnitTask(run_id="run-14", chunk_id="c", key=f"s3://bucket/{i}.tif", dataset_uri="s3://bucket/d.lance") for i in range(5)]
    # The stream already holds the first two, so it will report them as duplicates.
    dupes = {_dedupe_id(tasks[0]), _dedupe_id(tasks[1])}
    js = _FakeJS(dupes)
    queue = WorkQueue(cast("NatsClient", object()), cast("JetStreamContext", js))

    accepted = await queue.publish_units(tasks)

    assert len(js.published) == 5, "every unit must still be published — dedupe is decided by the stream, not skipped by us"
    assert accepted == 3, f"expected 3 newly-accepted units (2 of 5 were duplicates), got {accepted}"
