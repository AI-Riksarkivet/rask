"""I1 at the fetch boundary — the platform must know about SCHEMES, never about SOURCES.

The plane exists because IIIF was welded across twelve medallion files, which is why `S3PrefixSource`
sat written, unit-tested and unreachable for months. Round 1 then welded IIIF into the one place
EVERY http source must pass through: `_fetch_http` imported `storage.iiif.fetch_image`, so any HTTP
source inherited one particular source's client and retry policy.

Reading that function settles why it was the wrong reuse: its retry is entirely generic — transport
errors and 5xx retried with backoff, 4xx except 429 raised immediately. Nothing about it is IIIF; it
lives in `iiif.py` only because that is where it was first needed. So the fix is to implement the
generic policy generically, and leave the IIIF adapter the parts that ARE IIIF (the manifest read,
the URL construction) which already live in `ingest/adapters.py`.

The weld also hid a hard failure: `fetch_image`'s `client` is keyword-only and REQUIRED, and
`_fetch_http` never passed one, so every HTTP fetch raised TypeError before reaching the network.
Only `file://` and `s3://` were ever exercised.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from ingest.fetch import UriFetcher


@respx.mock
def test_a_5xx_is_RETRIED() -> None:
    """A server hiccup becomes a slowdown, not a lost unit.

    Measured behaviour against a real rate-limited endpoint: it hands out RST under load at ~64
    concurrent reads, and a single attempt turns that into a dropped page.
    """
    route = respx.get("https://example.org/flaky.jpg")
    route.side_effect = [httpx.Response(503), httpx.Response(503), httpx.Response(200, content=b"EVENTUALLY")]

    assert asyncio.run(UriFetcher().fetch("https://example.org/flaky.jpg")) == b"EVENTUALLY"
    assert route.call_count == 3


@respx.mock
def test_a_404_is_NOT_retried() -> None:
    """A dead page answers the same way every time.

    Retrying it spends the redelivery budget on a verdict already given, against the source the
    queue's backpressure exists to protect. The worker parks it on the first attempt.
    """
    route = respx.get("https://example.org/gone.jpg").mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(UriFetcher().fetch("https://example.org/gone.jpg"))

    assert route.call_count == 1, "a 404 was retried — no retry can turn a missing page into bytes"


def test_an_unknown_scheme_is_refused_by_NAME() -> None:
    """A worker that cannot fetch must say which scheme and which key — the message is the diagnosis,
    and it is also what the permanent/transient classifier keys on to park rather than redeliver."""
    with pytest.raises(ValueError, match="gopher"):
        asyncio.run(UriFetcher().fetch("gopher://old/1.tif"))
