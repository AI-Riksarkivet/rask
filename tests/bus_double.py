"""A process's connection to the bus, for a test: each publish is taken as the stream would take it, and kept.

A publish under test goes through the seam's own `lakehouse.bus.publish_event` (its size guard, its trace context, its
timeout) and ends here, where the real `Bus` hands the message to nats-py. What a real broker does with a connection
(its permission table, a session it ends, a duplicate it stores once) is driven on the seam's own tests and the
nats-auth lane; a test that takes this double is about what a publisher sends.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, override

from pydantic import BaseModel, ConfigDict

from lakehouse.bus import Bus, BusSettings
from lakehouse.supervision import Supervisor


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
    """Stands where `lakehouse.bus.Bus` does, connected and taking every publish; ``failing`` is raised for each one
    instead, as a bus that does not answer or refuses would, and ``down`` is a connection that is away."""

    def __init__(self, *, failing: Exception | None = None) -> None:
        super().__init__(BusSettings.model_construct(url="nats://bus.test:4222", user="test"))
        self.sent: list[Sent] = []
        self.failing = failing
        self.down = False

    @property
    def events(self) -> list[Any]:
        """The events published, in order, each as its producer signed it."""
        return [sent.event for sent in self.sent]

    @override
    async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:
        if self.failing is not None:
            raise self.failing
        self.sent.append(Sent(topic=topic, message_id=message_id, envelope=json.loads(body)))

    @property
    @override
    def connected(self) -> bool:
        """Up for as long as the test runs, unless it took the connection ``down``."""
        return not self.down


def session_holding(bus: BusDouble) -> Callable[..., AbstractAsyncContextManager[Bus | None]]:
    """What a service's lifespan opens in place of `lakehouse.bus.bus_session`, to hold ``bus`` for the length of its
    block: `monkeypatch.setattr(<the service's main>, "bus_session", session_holding(bus))`."""

    @asynccontextmanager
    async def session(_loops: Supervisor, _settings: BusSettings | None = None) -> AsyncGenerator[Bus | None]:
        yield bus

    return session
