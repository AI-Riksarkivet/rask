"""The NATS JetStream bus the estate's events ride, read and written as an outside client (XC-094).

Each event is published as a CloudEvent (`docs/COMMIT-EVENTS.md` § The envelope on the bus): a message's data is the
envelope, and the envelope's `data` is the event its producer signed. Every read here is by stream sequence (`$JS.API.STREAM.MSG.GET`), so a reader creates no
consumer on a stream the estate's subscribers share. A write is a plain publish, which each service pulls from its
consumer on its own connection exactly as it pulls a producer's. `declared` and `wait_until_acked` read a durable
consumer's info (`$JS.API.CONSUMER.INFO`) and change nothing.

THE CLIENT HOLDS NO BUS CREDENTIAL. A bus that takes anyone (the NATS fixture's default, `nats.auth.server` off, as the
e2e-stack lane runs it) is dialled with nothing. A bus in operator mode (`tls://`, the dev estate's) is dialled as its
services dial it: presenting a ServiceAccount token for the bus's audience (`LANCE_E2E_NATS_TOKEN_FILE`, minted by
`scripts/e2e_live.sh` with a TokenRequest) under the name of a session user the estate's values declare for these
suites (`LANCE_E2E_NATS_USER`), over TLS verified against the bus's CA (`LANCE_E2E_NATS_CA_FILE`) under the name its
certificate carries (`LANCE_E2E_NATS_TLS_NAME`, since the address is the Service's cluster IP). That user's row covers
the streams read and the subjects written here (deploy/values-e2e-suites.yaml, docs/OPERATORS.md § 7).
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nats
from nats.js.errors import NotFoundError
from pydantic import BaseModel


if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient


#: The bus, `nats://<host>:4222`, or `tls://<host>:4222` for one in operator mode.
NATS = os.environ.get("LANCE_E2E_NATS_URL", "")
#: What a bus in operator mode is presented with: the session user asked for, the file its ServiceAccount's token is
#: on, the bus's CA, and the name the bus's certificate carries.
NATS_USER = os.environ.get("LANCE_E2E_NATS_USER", "")
NATS_TOKEN_FILE = os.environ.get("LANCE_E2E_NATS_TOKEN_FILE", "")
NATS_CA_FILE = os.environ.get("LANCE_E2E_NATS_CA_FILE", "")
NATS_TLS_NAME = os.environ.get("LANCE_E2E_NATS_TLS_NAME", "")


class BusMessage(BaseModel):
    """One message a stream holds: where it sits, when the server stored it, the headers it was published with, and
    the CloudEvent it carries."""

    stream: str
    seq: int
    subject: str
    time: datetime
    headers: dict[str, str]
    envelope: dict[str, Any]

    @property
    def event(self) -> dict[str, Any]:
        """The event inside the envelope, as its producer published it."""
        data = self.envelope.get("data")
        return data if isinstance(data, dict) else {}


async def _connect() -> NatsClient:
    if not NATS.startswith("tls://"):
        return await nats.connect(NATS, connect_timeout=10)
    return await nats.connect(
        NATS,
        connect_timeout=10,
        token=Path(NATS_TOKEN_FILE).read_text(encoding="utf-8").strip(),
        name=NATS_USER,
        inbox_prefix=f"_INBOX_{NATS_USER}",
        tls=ssl.create_default_context(cafile=NATS_CA_FILE),
        tls_hostname=NATS_TLS_NAME,
        tls_handshake_first=True,
    )


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
            found.append(
                BusMessage(
                    stream=stream, seq=seq, subject=str(raw.subject), time=raw.time, headers=dict(raw.headers or {}), envelope=json.loads(raw.data or b"{}")
                )
            )
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


async def _declared(stream: str, consumer: str) -> bool:
    client = await _connect()
    try:
        await client.jetstream().consumer_info(stream, consumer)
    except NotFoundError:
        return False
    finally:
        await client.close()
    return True


def declared(stream: str, consumer: str) -> bool:
    """Whether ``stream`` holds the durable consumer ``consumer``: the chart's stream Job declares a lane's consumer only
    when the release runs that lane."""
    return asyncio.run(_declared(stream, consumer))


async def _acked(stream: str, consumers: Iterable[str], seq: int, deadline: float) -> dict[str, int]:
    client = await _connect()
    try:
        jetstream = client.jetstream()
        while True:
            floors = {consumer: (await jetstream.consumer_info(stream, consumer)).ack_floor.stream_seq for consumer in consumers}
            if all(floor >= seq for floor in floors.values()) or time.monotonic() > deadline:
                return floors
            await asyncio.sleep(0.5)
    finally:
        await client.close()


def wait_until_acked(stream: str, consumers: Iterable[str], seq: int, *, within: float) -> None:
    """Block until each durable consumer of ``stream`` has settled every message up to ``seq``.

    A service acknowledges a delivery once its handling has answered it, and ends one it parks, and either moves the
    consumer's ack floor past it; a delivery handed back keeps the floor below it until it is settled. So past this point
    each service has decided what to do with the message at ``seq``: recorded it, refused it, parked it, or found it
    already recorded.
    """
    floors = asyncio.run(_acked(stream, list(consumers), seq, time.monotonic() + within))
    assert all(floor >= seq for floor in floors.values()), f"{stream} message {seq} was not acknowledged within {within:.0f} s: ack floors {floors}"
