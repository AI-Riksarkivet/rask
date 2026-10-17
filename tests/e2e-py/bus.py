"""The NATS JetStream bus the estate's events ride, read and written as an outside client (XC-094).

Dapr publishes each event as a CloudEvent: a message's data is the envelope, and the envelope's `data` is the event
its producer signed. Every read here is by stream sequence (`$JS.API.STREAM.MSG.GET`), so a reader creates no
consumer on a stream the estate's subscribers share. A write is a plain publish, which each subscriber's sidecar
delivers to its app exactly as it delivers a producer's.

THE CLIENT IS ANONYMOUS unless `LANCE_E2E_NATS_CREDS` names a creds file. Both targets' bus takes a client with no
credential (the NATS fixture's default, `nats.auth.server` off); a bus in operator mode needs a user whose
permissions cover the streams read and the subjects written (docs/OPERATORS.md § 7).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import nats
from nats.js.errors import NotFoundError
from pydantic import BaseModel


if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient


#: The bus, `nats://<host>:4222`.
NATS = os.environ.get("LANCE_E2E_NATS_URL", "")
NATS_CREDS = os.environ.get("LANCE_E2E_NATS_CREDS", "")


class BusMessage(BaseModel):
    """One message a stream holds: where it sits, when the server stored it, and the CloudEvent it carries."""

    stream: str
    seq: int
    subject: str
    time: datetime
    envelope: dict[str, Any]

    @property
    def event(self) -> dict[str, Any]:
        """The event inside the envelope, as its producer published it."""
        data = self.envelope.get("data")
        return data if isinstance(data, dict) else {}


async def _connect() -> NatsClient:
    return await nats.connect(NATS, connect_timeout=10, user_credentials=NATS_CREDS or None)


async def _messages_since(stream: str, since: datetime) -> list[BusMessage]:
    client = await _connect()
    try:
        jetstream = client.jetstream()
        state = (await jetstream.stream_info(stream)).state
        found: list[BusMessage] = []
        # An empty stream reports sequence 0 at both ends, which no message holds: measured on a fresh JetStream,
        # asking for it answers 400 `bad request`.
        if not state.messages:
            return found
        for seq in range(state.last_seq, state.first_seq - 1, -1):
            try:
                raw = await jetstream.get_msg(stream, seq)
            except NotFoundError:
                # A sequence the stream no longer holds (a purge, a per-subject limit) is a hole, not the end.
                continue
            assert raw.time is not None, f"{stream} message {seq} carries no stored time"
            if raw.time < since:
                break
            found.append(BusMessage(stream=stream, seq=seq, subject=str(raw.subject), time=raw.time, envelope=json.loads(raw.data or b"{}")))
        return found[::-1]
    finally:
        await client.close()


def messages_since(stream: str, since: datetime) -> list[BusMessage]:
    """Every message ``stream`` holds that the server stored at or after ``since``, oldest first."""
    return asyncio.run(_messages_since(stream, since))


async def _last_seq(stream: str) -> int:
    client = await _connect()
    try:
        return (await client.jetstream().stream_info(stream)).state.last_seq
    finally:
        await client.close()


def last_seq(stream: str) -> int:
    """The sequence of the newest message ``stream`` holds; 0 while it holds none."""
    return asyncio.run(_last_seq(stream))


async def _publish(subject: str, envelope: dict[str, Any], msg_id: str | None) -> int:
    client = await _connect()
    try:
        ack = await client.jetstream().publish(subject, json.dumps(envelope).encode(), timeout=10, headers={"Nats-Msg-Id": msg_id} if msg_id else None)
    finally:
        await client.close()
    assert not ack.duplicate, f"the stream dropped the publish as a duplicate of an earlier one (Nats-Msg-Id {msg_id}), so nothing was delivered"
    return ack.seq


def publish(subject: str, envelope: dict[str, Any], *, msg_id: str | None) -> int:
    """Publish one CloudEvent on ``subject``; the stream sequence it was stored at.

    ``msg_id`` is the `Nats-Msg-Id` the stream deduplicates on within its window. ``None`` sends none, which is how a
    re-publish of a stored message reaches every subscriber again.
    """
    return asyncio.run(_publish(subject, envelope, msg_id))


async def _acked(stream: str, durables: Iterable[str], seq: int, deadline: float) -> dict[str, int]:
    client = await _connect()
    try:
        jetstream = client.jetstream()
        while True:
            floors = {durable: (await jetstream.consumer_info(stream, durable)).ack_floor.stream_seq for durable in durables}
            if all(floor >= seq for floor in floors.values()) or time.monotonic() > deadline:
                return floors
            await asyncio.sleep(0.5)
    finally:
        await client.close()


def wait_until_acked(stream: str, durables: Iterable[str], seq: int, *, within: float) -> None:
    """Block until each durable consumer of ``stream`` has acknowledged every message up to ``seq``.

    A subscriber's sidecar acknowledges a delivery once its app has answered it, so past this point each app has
    decided what to do with the message at ``seq``: recorded it, refused it, or found it already recorded.
    """
    floors = asyncio.run(_acked(stream, list(durables), seq, time.monotonic() + within))
    assert all(floor >= seq for floor in floors.values()), f"{stream} message {seq} was not acknowledged within {within:.0f} s: ack floors {floors}"
