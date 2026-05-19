"""Dead-letter queue handler — route failed messages for inspection/replay."""

from __future__ import annotations

import logging
from datetime import timedelta

import nats  # ty: ignore[unresolved-import]
from nats.js import JetStreamContext  # ty: ignore[unresolved-import]

log = logging.getLogger(__name__)

DLQ_STREAM = "etl-dlq"
DLQ_SUBJECT = "etl.dlq.>"


class DeadLetterHandler:
    """Route permanently-failed messages to a DLQ stream for inspection/replay.

    Messages in the DLQ include the original payload, the error message,
    and the original subject for targeted replay.
    """

    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    @classmethod
    async def create(cls, nc: nats.NATS) -> DeadLetterHandler:
        """Create the DLQ handler, ensuring the DLQ stream exists."""
        js = nc.jetstream()
        await js.find_stream_name_by_subject(DLQ_SUBJECT)
        return cls(js)

    async def send(
        self,
        subject: str,
        payload: bytes,
        error: str,
    ) -> None:
        """Send a failed message to the DLQ."""
        import json

        dlq_payload = json.dumps(
            {
                "original_subject": subject,
                "payload": payload.decode("utf-8", errors="replace"),
                "error": error,
            }
        ).encode()
        dlq_subject = f"etl.dlq.{subject}"
        await self._js.publish(dlq_subject, dlq_payload)
        log.warning("Message sent to DLQ: %s — %s", subject, error)

    async def replay(self, *, filter_subject: str | None = None) -> int:
        """Replay DLQ messages back to their original subjects.

        Returns the number of messages replayed.
        """
        import json

        sub_subject = f"etl.dlq.{filter_subject}" if filter_subject else DLQ_SUBJECT
        sub = await self._js.subscribe(
            sub_subject, stream=DLQ_STREAM, ordered_consumer=True
        )

        count = 0
        async for msg in sub.messages:
            try:
                data = json.loads(msg.data)
                original_subject = data["original_subject"]
                original_payload = data["payload"].encode()
                await self._js.publish(original_subject, original_payload)
                count += 1
            except Exception:
                log.exception("Failed to replay DLQ message")
            # Stop after draining existing messages
            if msg.pending == 0:
                break

        await sub.unsubscribe()
        log.info("Replayed %d DLQ messages", count)
        return count

    async def purge(self, *, older_than: timedelta | None = None) -> int:
        """Purge DLQ messages. Returns count purged."""
        info = await self._js.stream_info(DLQ_STREAM)
        count = info.state.messages
        await self._js.purge_stream(DLQ_STREAM)
        log.info("Purged %d DLQ messages", count)
        return count
