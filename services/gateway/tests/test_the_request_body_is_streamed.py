"""The gateway PROXIES a request body; it does not collect one first (GW-BUFFERS-REQUEST-BODY).

`content=await request.body()` materialised the whole upload in the gateway's memory before a single
byte reached the upstream. The gateway fronts `/api/ingest` (the ingest control plane),
`/api/explorer/annotations` (the annotator's Arrow-IPC writes) and `/api/produce` — the three planes
whose payloads are the least bounded — so a handful of concurrent large POSTs multiply straight into
the pod's memory limit, and every one of them pays full-upload latency before the upstream sees
anything. The response half was already streamed (`StreamingResponse(upstream_resp.aiter_raw())`),
which is the asymmetry that showed the primitive was understood here.

The proof is a deadlock, not a measurement. The client releases the SECOND half of its body only
after the upstream has been reached; a gateway that buffers can never reach the upstream, so the
request never completes. A gateway that streams reaches it on the first chunk and the request
finishes. No byte counting, no memory sampling, nothing that could pass by accident.

`content-length` is pinned alongside, because the streaming form is the one that can lose it: httpx
stamps `Transfer-Encoding: chunked` on an async-iterator body unless a length is already declared,
and a chunked re-frame of a request the client sized is a different request.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


class _Streamed(httpx.AsyncByteStream):
    """A response body the gateway can `aiter_raw()` — `Response(content=...)` is pre-read."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data

    async def aclose(self) -> None:
        return None


class _Upstream(httpx.AsyncBaseTransport):
    """An upstream that announces the moment it is reached, then drains the request stream."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.body: bytes | None = None
        self.headers: httpx.Headers | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.reached.set()
        self.headers = request.headers
        # `cast`: `Request.stream` is typed as the sync/async union; an AsyncClient always builds
        # the async one, and only that one is iterable here.
        chunks = [chunk async for chunk in cast(httpx.AsyncByteStream, request.stream)]
        self.body = b"".join(chunks)
        return httpx.Response(200, stream=_Streamed(b"ok"), headers={"content-length": "2"}, request=request)


def _wired(gw, upstream: _Upstream) -> None:
    """Everything the lifespan would have put on `app.state` — ASGITransport runs no lifespan."""
    settings = gw.build_gateway_settings()
    gw.app.state.settings = settings
    gw.app.state.api_prefix = settings.api_prefix
    gw.app.state.routes = gw._routes(settings)
    gw.app.state.shutting_down = False
    gw.app.state.startup_complete = True
    gw.app.state.http = httpx.AsyncClient(transport=upstream)


@pytest.mark.asyncio
async def test_the_upstream_is_reached_before_the_body_has_finished_arriving(gw) -> None:
    upstream = _Upstream()
    _wired(gw, upstream)

    first, second = b"A" * 4096, b"B" * 4096

    async def client_body() -> AsyncIterator[bytes]:
        yield first
        # The client is still uploading. A gateway that buffers is waiting for THIS chunk, and this
        # chunk is waiting for the upstream it will never contact.
        await upstream.reached.wait()
        yield second

    transport = httpx.ASGITransport(app=gw.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        try:
            response = await asyncio.wait_for(
                client.post(
                    "/api/produce",
                    content=client_body(),
                    headers={"content-length": str(len(first) + len(second)), "content-type": "application/octet-stream"},
                ),
                timeout=5.0,
            )
        except TimeoutError:  # pragma: no cover — this IS the red state
            pytest.fail("the gateway never reached the upstream: it was still buffering the request body")

    assert response.status_code == 200
    assert upstream.body == first + second


@pytest.mark.asyncio
async def test_the_forwarded_body_keeps_its_declared_length(gw) -> None:
    """A sized request stays sized: no silent chunked re-frame of somebody else's upload."""
    upstream = _Upstream()
    _wired(gw, upstream)

    payload = b"C" * 2048
    transport = httpx.ASGITransport(app=gw.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post("/api/produce", content=payload)

    assert response.status_code == 200
    assert upstream.body == payload
    assert upstream.headers is not None
    assert upstream.headers.get("content-length") == str(len(payload))
    assert "transfer-encoding" not in upstream.headers


@pytest.mark.asyncio
async def test_a_bodyless_get_is_not_re_framed_as_chunked(gw) -> None:
    """A GET carries no body and must not acquire a `Transfer-Encoding` on the way through.

    `request.stream()` yields an empty chunk for a bodyless request, and httpx reads an async
    iterator with no declared length as chunked — so the naive streaming fix turns every proxied GET
    into a chunked request, which some upstreams and the Dapr invoke hop answer differently.
    """
    upstream = _Upstream()
    _wired(gw, upstream)

    transport = httpx.ASGITransport(app=gw.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.get("/api/produce")

    assert response.status_code == 200
    assert upstream.body == b""
    assert upstream.headers is not None
    assert "transfer-encoding" not in upstream.headers
