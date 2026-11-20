"""A process's connection to the bus, for a test: each publish is taken as the stream would take it, and kept, and a
delivery is handed to the service as the bus would hand it.

A publish under test goes through the seam's own `lakehouse.events.bus.publish_event` (its size guard, its trace context, its
timeout) and ends here, where the real `Bus` hands the message to nats-py. A delivery under test starts here, where the
real `Bus` takes a message from nats-py: the service's own handling is handed it and the seam's own rule settles it
(`lakehouse.events.bus_delivery.judged`), so a test reads what the bus would be told. What a real broker does with a
connection (its permission table, a session it ends, a duplicate it stores once, a message it delivers again after a
delay) is driven on the seam's own tests and the nats-auth lane; a test that takes this double is about what a
publisher sends and what a consumer does with what it is handed.

What was taken is also the stream a reader reads by sequence (`read`): the n-th message taken, on any topic, is sequence
n, and `expire_through` drops the front as a stream's retention does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final, override

from pydantic import BaseModel, ConfigDict

from lakehouse.events.bus import Bus, BusSettings, Follow, StreamPage
from lakehouse.events.bus_delivery import Delivery, Handle, Lane, Park, Settlement, judged
from lakehouse.process.supervision import Supervisor


#: How long a delivery waits for the service's lifespan to have started the consumer it is for.
_STARTED_WITHIN_SECONDS: Final = 5.0


class Sent(BaseModel):
    """One message the bus took: on which topic, under which message id, and the envelope it carried."""

    model_config = ConfigDict(frozen=True)

    topic: str
    message_id: str
    envelope: dict[str, Any]

    @property
    def event(self) -> Any:
        """The event the envelope carries, as its producer signed it."""
        return self.envelope["data"]


class BusDouble(Bus):
    """Stands where `lakehouse.events.bus.Bus` does, connected and taking every publish; ``failing`` is raised for each one
    instead, as a bus that does not answer or refuses would, and ``down`` is a connection that is away."""

    def __init__(self, *, failing: Exception | None = None) -> None:
        super().__init__(BusSettings.model_construct(url="nats://bus.test:4222", user="test"))
        self.sent: list[Sent] = []
        self.failing = failing
        self.down = False
        # The double stands for a process that holds its connection, so a readiness read judges it at once.
        self._held.set()
        self._consumers: dict[str, tuple[Lane, Handle, Park]] = {}
        self._follows: dict[str, Follow] = {}
        self._handed_count = 0
        self._expired_through = 0

    @property
    def events(self) -> list[Any]:
        """The events published, in order, each as its producer signed it."""
        return [sent.event for sent in self.sent]

    @override
    async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:
        if self.failing is not None:
            raise self.failing
        self.sent.append(Sent(topic=topic, message_id=message_id, envelope=json.loads(body)))

    def take(self, topic: str, envelope: Mapping[str, Any]) -> None:
        """Take ``envelope`` on ``topic`` as another process's publish would be taken: it becomes the stream's next message."""
        self.sent.append(Sent(topic=topic, message_id=f"sent-{len(self.sent) + 1}", envelope=dict(envelope)))

    def expire_through(self, sequence: int) -> None:
        """Drop every message up to ``sequence`` from the stream, as its retention would."""
        self._expired_through = max(self._expired_through, sequence)

    @override
    async def read(self, stream: str, subject: str, *, after: int, limit: int) -> StreamPage:
        """What the stream of what was taken holds on ``subject`` after ``after``, paged by `lakehouse.events.bus.StreamPage`'s
        own rules: the subject's end is its last message the stream still holds, as the bus answers it."""
        held = [(n, sent) for n, sent in enumerate(self.sent, start=1) if sent.topic == subject and n > self._expired_through]
        end = held[-1][0] if held else 0
        place = StreamPage.first_place(after, end, limit)
        page = [] if place is None else [(n, sent) for n, sent in held if n > place][:limit]
        messages = [Delivery(subject=subject, body=json.dumps(sent.envelope).encode(), sequence=n, attempt=1) for n, sent in page]
        return StreamPage.of(after, end, messages)

    @property
    @override
    def connected(self) -> bool:
        """Up for as long as the test runs, unless it took the connection ``down``."""
        return not self.down

    @override
    async def consume(self, lane: Lane, handle: Handle, *, park: Park) -> None:
        """Hold the service's handling of ``lane`` for :meth:`deliver`, until cancelled as the real loop is."""
        self._consumers[lane.consumer] = (lane, handle, park)
        await asyncio.Event().wait()

    @override
    async def follow(self, stream: str, subject: str, handle: Follow) -> None:
        """Hold the service's handling of ``subject`` for :meth:`broadcast`, until cancelled as the real loop is."""
        self._follows[subject] = handle
        await asyncio.Event().wait()

    async def deliver(self, consumer: str, envelope: Mapping[str, Any] | bytes, *, attempt: int = 1, subject: str = "") -> Settlement:
        """Hand ``envelope`` to the service's handling of ``consumer`` as that message's ``attempt``-th delivery, and
        answer what the bus would be told about it. Run on the service's own loop: `client.portal.call(bus.deliver, ...)`
        under a `TestClient`.

        Raises:
            TimeoutError: The service started no consumer of that name.
        """
        lane, handle, park = self._consumers[await self._started(self._consumers, consumer)]
        return await judged(self._delivery(envelope, subject=subject, attempt=attempt), lane, handle, park)

    async def lane_of(self, consumer: str) -> Lane:
        """The lane the service consumes ``consumer`` with: the numbers it asked the bus to settle its deliveries by.

        Raises:
            TimeoutError: The service started no consumer of that name.
        """
        return self._consumers[await self._started(self._consumers, consumer)][0]

    async def broadcast(self, subject: str, envelope: Mapping[str, Any] | bytes) -> None:
        """Hand ``envelope`` to the service's handling of the followed ``subject``; what that handling raises is raised
        here, where the real loop would say it and hand on the next message.

        Raises:
            TimeoutError: The service follows no such subject.
        """
        handle = self._follows[await self._started(self._follows, subject)]
        await handle(self._delivery(envelope, subject=subject, attempt=1))

    def _delivery(self, envelope: Mapping[str, Any] | bytes, *, subject: str, attempt: int) -> Delivery:
        self._handed_count += 1
        body = envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode()
        return Delivery(subject=subject, body=body, sequence=self._handed_count, attempt=attempt)

    @staticmethod
    async def _started(held: Mapping[str, object], name: str) -> str:
        """``name``, once the service's lifespan has started the loop that holds it."""
        async with asyncio.timeout(_STARTED_WITHIN_SECONDS):
            while name not in held:
                await asyncio.sleep(0.01)
        return name


def session_holding(bus: BusDouble) -> Callable[..., AbstractAsyncContextManager[Bus | None]]:
    """What a service's lifespan opens in place of `lakehouse.events.bus.bus_session`, to hold ``bus`` for the length of its
    block: `monkeypatch.setattr(<the service's main>, "bus_session", session_holding(bus))`."""

    @asynccontextmanager
    async def session(_loops: Supervisor, _settings: BusSettings | None = None) -> AsyncGenerator[Bus | None]:
        yield bus

    return session
