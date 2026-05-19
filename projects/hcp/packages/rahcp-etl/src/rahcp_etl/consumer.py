"""JetStream durable consumer for ETL work items."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import nats  # ty: ignore[unresolved-import]
from nats.js.api import ConsumerConfig, DeliverPolicy  # ty: ignore[unresolved-import]

log = logging.getLogger(__name__)


class ETLConsumer:
    """Durable JetStream consumer with auto-reconnect.

    Connects to NATS, binds a durable consumer on the given stream/subject,
    and dispatches messages to the handler. Acks on success, naks on failure.
    """

    def __init__(
        self,
        nats_url: str,
        stream: str,
        subject: str,
        durable: str,
        *,
        max_deliver: int = 5,
        ack_wait: float = 30.0,
    ) -> None:
        self.nats_url = nats_url
        self.stream = stream
        self.subject = subject
        self.durable = durable
        self.max_deliver = max_deliver
        self.ack_wait = ack_wait
        self._nc: nats.NATS | None = None
        self._sub: Any = None
        self._running = False

    async def start(self, handler: Callable) -> None:
        """Connect and start consuming messages.

        ``handler`` receives the message payload as bytes and should
        return a dict (success) or raise an exception (triggers nak/retry).
        """
        self._nc = await nats.connect(
            self.nats_url,
            reconnected_cb=self._on_reconnect,
            disconnected_cb=self._on_disconnect,
        )
        js = self._nc.jetstream()

        config = ConsumerConfig(
            durable_name=self.durable,
            deliver_policy=DeliverPolicy.ALL,
            max_deliver=self.max_deliver,
            ack_wait=self.ack_wait,
        )

        self._sub = await js.subscribe(
            self.subject,
            stream=self.stream,
            config=config,
        )
        self._running = True
        log.info(
            "ETL consumer started: stream=%s subject=%s durable=%s",
            self.stream,
            self.subject,
            self.durable,
        )

        async for msg in self._sub.messages:
            if not self._running:
                break
            try:
                await handler(msg.data)
                await msg.ack()
            except Exception:
                log.exception("Handler failed for message on %s", msg.subject)
                await msg.nak()

    async def stop(self) -> None:
        """Stop consuming and disconnect."""
        self._running = False
        if self._sub:
            await self._sub.unsubscribe()
        if self._nc:
            await self._nc.close()
        log.info("ETL consumer stopped")

    async def _on_reconnect(self) -> None:
        log.warning("NATS reconnected to %s", self.nats_url)

    async def _on_disconnect(self) -> None:
        log.warning("NATS disconnected from %s", self.nats_url)
