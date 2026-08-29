"""`dashboard.proxy` returns the httpx-DECODED body — so its headers must not describe the wire.

httpx transparently decompresses a `content-encoding: gzip` response: `resp.content` is the decoded
bytes, while `resp.headers` still carries the origin's `content-encoding` and `content-length` — both
describing the COMPRESSED transfer. Relaying those alongside the decoded content makes any consumer
that forwards them (the compute Serve proxy did, verbatim) hand a browser plaintext it then tries to
re-inflate (a DecodingError) or a length that does not match the body. The headers are false at THIS
seam's own boundary, so this seam strips them — not each consumer.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from ray_kit import dashboard


@pytest.mark.asyncio
async def test_proxy_drops_the_transfer_headers_of_the_body_it_decoded() -> None:
    payload = b'{"applications": {}}'
    compressed = gzip.compress(payload)

    def _gzipped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=compressed,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_gzipped)) as http:
        resp = await dashboard.proxy(http, "http://ray:8265", "api/serve/applications/", "GET", "", {}, b"")

    assert resp.content == payload, "httpx should have handed proxy the DECODED body"
    lowered = {k.lower() for k in resp.headers}
    assert "content-encoding" not in lowered, "proxy relays a content-encoding describing bytes it does not return"
    assert "content-length" not in lowered, "proxy relays the COMPRESSED length for a decoded body"
    assert resp.headers.get("content-type") == "application/json"  # real body metadata still travels
