"""A post to lineage's intake is refused from its headers, before a byte of its body is read (XC-201, CTL-006).

The intake (`POST /api/v1/lineage`) takes an OpenLineage event as a JSON body, and FastAPI reads and parses a route's body
before it solves any dependency, the bearer check included. So lineage's app is built with a body cap that refuses a
body declared over it from its Content-Length (413), and its caller is judged from the headers before the body is read
(`lakehouse.admission`), so a post with no token is refused 401 whatever it would send. Driven over a socket to a live
uvicorn, as a client reaches it: only the headers are sent, and the answer must arrive while the body is still unsent.
Authentication is on, against the loopback Dex in the root conftest; the provenance store is one whose feed answers at
once, since the claim is about the door.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn

from lineage import main
from lineage.core.config import get_settings


#: How long a client waits for an answer to its headers alone; a server waiting for the body sends none.
_WAIT_SECONDS = 5.0


class _Feed:
    """A provenance store whose feed answers at once."""

    async def ensure(self) -> None:
        return None

    async def ready(self) -> None:
        return None


@pytest.fixture
def lineage_port(dex_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    for key, value in {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "rask-lineage",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr(main.ProvenanceStore, "from_settings", lambda _settings: _Feed())
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, lifespan="on", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("lineage's uvicorn did not start")
        time.sleep(0.01)
    yield int(server.servers[0].sockets[0].getsockname()[1])
    server.should_exit = True
    thread.join(timeout=10)


def _answer_to_the_headers_alone(port: int, content_length: int) -> int | None:
    """The status lineage answers a post to its intake of which only the headers were sent, with no token; ``None`` when
    it answers nothing, waiting for the body."""
    head = f"POST /api/v1/lineage HTTP/1.1\r\nHost: lineage\r\nContent-Type: application/json\r\nContent-Length: {content_length}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=_WAIT_SECONDS) as conn:
        conn.sendall(head.encode())
        try:
            status_line = conn.recv(4096).split(b"\r\n", 1)[0]
        except TimeoutError:
            return None
    return int(status_line.split(b" ", 2)[1])


@pytest.mark.parametrize(
    ("content_length", "status"),
    [
        pytest.param(64 * 1024 * 1024, 413, id="64-mib-over-the-cap"),
        pytest.param(64 * 1024, 401, id="under-the-cap-with-no-token"),
    ],
)
def test_an_unauthenticated_post_is_refused_before_its_body_is_sent(lineage_port: int, content_length: int, status: int) -> None:
    assert _answer_to_the_headers_alone(lineage_port, content_length) == status
