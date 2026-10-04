"""Ingest connects to NATS as its own user, read from its own sidecar's secret store at startup ([[XC-078]]).

Every other app reaches NATS through its Dapr sidecar, whose component carries that app's user. Ingest's work queue is a
direct nats-py client, so each of its two connect seams presents ingest's user itself: `WorkQueue.connect`, which every
activity body connects through, and `inspect_queue`, behind `GET /queue`. Driven through the real app and its real
lifespan, which reads `nats-user-ingest` through the sidecar's secret API (answered here by respx).

THE BROKER STANDS IN for an operator-mode nats-server, which no offline lane runs. It speaks the handshake and judges each
CONNECT as the server's authentication does: it requires authentication, sends a fresh nonce, and admits the connection
only when it presents the user JWT and an Ed25519 signature over that nonce made with the key the JWT names. Past the
handshake it answers every request as JetStream does for a stream that does not exist, so the probe completes. The real
server, the permission table and a run drained under ingest's own user are the operator-mode lane's claim.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import socketserver
import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

import ingest
from ingest.queue import BrokerUnreachable, WorkQueue
from ingest.runtime import nats_url


if TYPE_CHECKING:
    from fastapi import FastAPI


NATS_USER = "http://localhost:3500/v1.0/secrets/lance-secrets/nats-user-ingest"

#: The account's signed claim about the user. A client presents it verbatim, so its content is opaque here.
USER_JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJlZDI1NTE5LW5rZXkifQ.ingest-user-claims.account-signature"

#: JetStream's answer to a request about a stream that does not exist.
STREAM_NOT_FOUND = json.dumps(
    {"type": "io.nats.jetstream.api.v1.stream_info_response", "error": {"code": 404, "err_code": 10059, "description": "stream not found"}}
).encode()


class _OperatorModeBroker(socketserver.ThreadingTCPServer):
    """A NATS server in operator mode, as far as a client's handshake can tell. Records its verdict on every CONNECT."""

    daemon_threads = True
    #: A refused client may leave its socket open; closing the server must not wait on that connection's thread.
    block_on_close = False

    def __init__(self, user_public: Ed25519PublicKey) -> None:
        super().__init__(("127.0.0.1", 0), _Connection)
        self.user_public = user_public
        self.verdicts: list[str] = []

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"nats://{host!s}:{port}"

    def judge(self, connect: dict[str, Any], nonce: str) -> str:
        """The server's answer to one CONNECT: the user's JWT, and this connection's nonce signed by the key it names."""
        if "jwt" not in connect:
            return "no user JWT"
        if connect["jwt"] != USER_JWT:
            return "a user JWT that is not ingest's"
        sig = str(connect.get("sig", ""))
        try:
            # nats-server reads `sig` as unpadded URL-safe base64 or as standard base64; this decode accepts both.
            self.user_public.verify(base64.b64decode(sig + "=" * (-len(sig) % 4), altchars=b"-_", validate=True), nonce.encode())
        except (InvalidSignature, ValueError):
            return "a signature that does not verify over the nonce"
        return "admitted"


class _Connection(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        broker = cast("_OperatorModeBroker", self.server)
        # As nats-server makes it: 11 random bytes in unpadded URL-safe base64. The signed bytes are this text.
        nonce = secrets.token_urlsafe(11)
        info = {"server_id": "operator-mode", "version": "2.14.2", "proto": 1, "headers": True, "max_payload": 1048576, "auth_required": True, "nonce": nonce}
        self._send(b"INFO " + json.dumps(info).encode())
        verdict = broker.judge(json.loads(self.rfile.readline().removeprefix(b"CONNECT ")), nonce)
        broker.verdicts.append(verdict)
        if verdict != "admitted":
            self._send(b"-ERR 'Authorization Violation'")
            return
        inbox_sid = b""
        for line in self.rfile:
            op, *args = line.split() or [b""]
            if op == b"PING":
                self._send(b"PONG")
            elif op == b"SUB":
                inbox_sid = args[-1]
            elif op in (b"PUB", b"HPUB"):
                self.rfile.read(int(args[-1]) + 2)
                if len(args) == (3 if op == b"PUB" else 4):
                    self._send(b"MSG %s %s %d\r\n%s" % (args[1], inbox_sid, len(STREAM_NOT_FOUND), STREAM_NOT_FOUND))

    def _send(self, line: bytes) -> None:
        self.wfile.write(line + b"\r\n")


@pytest.fixture
def nats_user(event_signer: Any) -> Any:
    """Ingest's NATS user: an NKEY user seed (`SU...`) and its public key (`U...`), as the dev mint provisions them."""
    return event_signer("nats-user-ingest")


@pytest.fixture
def broker(nats_user: Any) -> Iterator[_OperatorModeBroker]:
    # A public NKEY is base32 of one type byte, the 32-byte Ed25519 key and a 2-byte checksum.
    server = _OperatorModeBroker(Ed25519PublicKey.from_public_bytes(base64.b32decode(nats_user.public)[1:33]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def ingest_app(secret_store_lifespan: None, broker: _OperatorModeBroker, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("RASK_SIGNING_IDENTITY", raising=False)
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_NATS_URL", broker.url)
    return ingest.create_app()


def _the_queue_probe(client: TestClient) -> None:
    client.get("/api/queue")


def _the_work_queue(_client: TestClient) -> None:
    async def connect() -> None:
        with contextlib.suppress(BrokerUnreachable):
            queue = await WorkQueue.connect(nats_url())
            await queue.close()

    asyncio.run(connect())


@pytest.mark.parametrize("connect", [pytest.param(_the_queue_probe, id="the-queue-probe"), pytest.param(_the_work_queue, id="the-work-queue")])
def test_each_connect_seam_presents_ingests_nats_user(
    connect: Callable[[TestClient], None], ingest_app: FastAPI, broker: _OperatorModeBroker, nats_user: Any
) -> None:
    with respx.mock:
        respx.get(NATS_USER).mock(return_value=httpx.Response(200, json={"jwt": USER_JWT, "seed": nats_user.seed}))
        with TestClient(ingest_app) as client:
            connect(client)

        assert broker.verdicts == ["admitted"], f"the broker's verdict on each CONNECT ingest made: {broker.verdicts}"
