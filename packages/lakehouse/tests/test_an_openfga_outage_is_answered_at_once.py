"""An OpenFGA that has stopped answering is refused at once, not waited on by every request.

Each call retries a transient failure and then fails closed with a 503. Without a circuit, every request in an outage
pays the whole retry budget while the pod stays Ready. Once ``fga_resilience.BREAKER_THRESHOLD`` calls in a row have ended in an
outage, the next is refused without dialling, and once ``fga_resilience.BREAKER_RESET_SECONDS`` have passed a call is let through
whose answer closes the circuit again.

The client is the SDK's own ``OpenFgaClient`` against a server speaking OpenFGA's wire shape, so what is counted is
requests that reach the server.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient

from lakehouse.governed import fga_provision, fga_reads, fga_resilience


class _OpenFga:
    """An OpenFGA whose Check answers 503 while ``down`` and ``allowed`` once it is up, counting what reaches it."""

    def __init__(self) -> None:
        self.down = True
        self.requests = 0

    async def check(self, request: web.Request) -> web.Response:
        del request
        self.requests += 1
        if self.down:
            return web.json_response({"code": "internal_error", "message": "unavailable"}, status=503)
        return web.json_response({"allowed": True, "resolution": ""})


_ATTEMPTS = 2
_NO_WAIT = fga_resilience.RetryPolicy(attempts=_ATTEMPTS, backoff_seconds=0.0, max_backoff_seconds=0.0)


async def _ask(client: OpenFgaClient) -> bool:
    return await fga_reads.check(client, user="dex~alice", relation="can_read", obj="table:t", retry=_NO_WAIT)


@pytest.mark.asyncio
async def test_an_outage_is_refused_without_dialling_once_the_circuit_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    openfga = _OpenFga()
    app = web.Application()
    app.router.add_post("/stores/{store}/check", openfga.check)
    async with TestServer(app) as server:
        client = fga_provision.make_client(str(server.make_url("")).rstrip("/"), "01HX5T0RE0000000000000000A", "01HXM0DE10000000000000000B", token_file=None)
        try:
            for _ in range(fga_resilience.BREAKER_THRESHOLD):
                with pytest.raises(ServiceUnavailableError):
                    await _ask(client)
            dialled = openfga.requests
            assert dialled == _ATTEMPTS * fga_resilience.BREAKER_THRESHOLD

            with pytest.raises(ServiceUnavailableError, match=r"^authorization service unavailable$"):
                await _ask(client)
            assert openfga.requests == dialled, "an open circuit dialled OpenFGA"

            openfga.down = False
            monkeypatch.setattr(fga_resilience, "BREAKER_RESET_SECONDS", 0.0)
            assert await _ask(client) is True
            assert await _ask(client) is True
            assert openfga.requests == dialled + 2
        finally:
            await client.close()
