"""A sidecar whose bus connection closed for good is restarted once the bus answers again, and only then ([[XC-095]]).

A sidecar restarted while the bus refuses connections fails its pub/sub component and exits, so the restart waits for
the bus; a connection that is live or still reconnecting is never restarted.

DRIVEN AGAINST THE SIDECAR'S HTTP API, which respx answers with the bodies daprd 1.18.1 sends for a publish
(`pkg/api/errors/pubsub.go:78`, wrapping nats.go v1.39.1's errors): the claim is what the watchdog does with each answer
the sidecar can give. The bus is a real TCP server on the loopback, greeting a connection as a NATS server does, or a
port nothing listens on.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx

from service_kit.bus_watchdog import BusWatchdog


SIDECAR = "http://localhost:3500"
APP_ID = "catalog"
#: What daprd's `/v1.0/metadata` lists for the catalog's sidecar: two bus components and a store the probe passes by.
METADATA = {
    "id": APP_ID,
    "components": [
        {"name": "pubsub", "type": "pubsub.jetstream", "version": "v1"},
        {"name": "catalog-control-pubsub", "type": "pubsub.jetstream", "version": "v1"},
        {"name": "statestore", "type": "state.postgresql", "version": "v1"},
    ],
}


def _publish_failed(component: str, cause: str) -> httpx.Response:
    return httpx.Response(
        500,
        json={"errorCode": "ERR_PUBSUB_PUBLISH_MESSAGE", "message": f"error when publishing to topic probe.{APP_ID} in pubsub {component}: {cause}"},
    )


class _Sidecar:
    """The sidecar's answers: its metadata, a probe through each bus component, and the shutdown it may be asked for."""

    def __init__(self) -> None:
        respx.get(f"{SIDECAR}/v1.0/metadata").mock(return_value=httpx.Response(200, json=METADATA))
        self.probes = {name: respx.post(f"{SIDECAR}/v1.0/publish/{name}/probe.{APP_ID}") for name in ("pubsub", "catalog-control-pubsub")}
        self.shutdown = respx.post(f"{SIDECAR}/v1.0/shutdown").mock(return_value=httpx.Response(204))

    def answer(self, control_cause: str) -> None:
        """The lineage component is live; the control component answers ``control_cause``."""
        self.probes["pubsub"].mock(return_value=_publish_failed("pubsub", "nats: no response from stream"))
        self.probes["catalog-control-pubsub"].mock(return_value=_publish_failed("catalog-control-pubsub", control_cause))


@pytest.fixture
def sidecar(respx_allows_unused_routes: None) -> Iterator[_Sidecar]:
    with respx.mock:
        yield _Sidecar()


@asynccontextmanager
async def _bus(*, greets: bool) -> AsyncGenerator[tuple[str, int]]:
    """A server greeting each connection as NATS does, or a loopback port nothing listens on."""
    if not greets:
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            address = unused.getsockname()[:2]
        yield address
        return

    async def greet(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'INFO {"server_id":"rask-nats-0","version":"2.14.2","jetstream":true}\r\n')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(greet, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_cause", "greets", "restarted"),
    [
        pytest.param("nats: connection closed", True, True, id="closed-and-the-bus-answers"),
        pytest.param("nats: connection closed", False, False, id="closed-while-the-bus-is-gone"),
        pytest.param("nats: no response from stream", True, False, id="live"),
        pytest.param("nats: timeout", False, False, id="still-reconnecting"),
    ],
)
async def test_a_sidecar_is_restarted_only_once_a_closed_connection_can_connect_again(
    sidecar: _Sidecar, control_cause: str, greets: bool, restarted: bool
) -> None:
    sidecar.answer(control_cause)

    async with _bus(greets=greets) as server:
        watchdog = BusWatchdog(sidecar=httpx.AsyncClient(base_url=SIDECAR), servers=[server], spread=0.0)
        try:
            asked = await watchdog.check()
        finally:
            await watchdog.stop()

    assert (asked, sidecar.shutdown.called) == (restarted, restarted)
