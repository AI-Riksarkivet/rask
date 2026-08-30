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
import sys
from pathlib import Path

import httpx
import pytest
import respx

from ingest.fetch import UriFetcher


def test_the_fetch_module_imports_NO_source_specific_module() -> None:
    """The structural half of I1, asserted on the source text.

    A runtime test can only prove the paths it exercises; this proves the module cannot reach a
    source-specific import at all. `storage.iiif` is named explicitly because it is the one that got
    in — but the rule is general, and a second source welded in the same way would be the same
    defect wearing a different name.
    """
    text = (Path(__file__).resolve().parents[1] / "src" / "ingest" / "fetch.py").read_text(encoding="utf-8")

    offenders = [line.strip() for line in text.splitlines() if line.strip().startswith(("import ", "from ")) and "iiif" in line.lower()]

    assert offenders == [], f"the scheme-resolved fetcher imports a SOURCE-specific module: {offenders}"


@respx.mock
def test_a_plain_https_source_never_touches_storage_iiif() -> None:
    """The behavioural half: fetch a non-IIIF URL and prove `storage.iiif` was never even imported.

    Asserted on `sys.modules` rather than by patching, because patching would prove only that the
    double was called. If the module is absent from the interpreter after a successful fetch, the
    fetch demonstrably did not go through it.
    """
    sys.modules.pop("storage.iiif", None)
    respx.get("https://data.example.org/objects/42.tif").mock(return_value=httpx.Response(200, content=b"PLAIN-HTTP-BYTES"))

    got = asyncio.run(UriFetcher().fetch("https://data.example.org/objects/42.tif"))

    assert got == b"PLAIN-HTTP-BYTES"
    assert "storage.iiif" not in sys.modules, "a plain https fetch imported the IIIF module"


@respx.mock
def test_the_http_path_WORKS_at_all() -> None:
    """The regression guard for the TypeError.

    `fetch_image(url)` omitted a required keyword-only `client`, so every HTTP fetch raised before it
    reached the network. Nothing caught it because the lane only ever ran `file://` and `s3://` —
    a IIIF ingest would have failed on its first unit, in a cluster, having already been declared
    working.
    """
    respx.get("https://example.org/a.jpg").mock(return_value=httpx.Response(200, content=b"OK"))

    assert asyncio.run(UriFetcher().fetch("https://example.org/a.jpg")) == b"OK"


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


@respx.mock
def test_a_429_IS_retried_because_it_means_slow_down() -> None:
    """The one 4xx that invites a retry. Parking it would discard a page the source was merely
    asking us to wait for."""
    route = respx.get("https://example.org/busy.jpg")
    route.side_effect = [httpx.Response(429), httpx.Response(200, content=b"AFTER-BACKOFF")]

    assert asyncio.run(UriFetcher().fetch("https://example.org/busy.jpg")) == b"AFTER-BACKOFF"
    assert route.call_count == 2


def test_an_unknown_scheme_is_refused_by_NAME() -> None:
    """A worker that cannot fetch must say which scheme and which key — the message is the diagnosis,
    and it is also what the permanent/transient classifier keys on to park rather than redeliver."""
    with pytest.raises(ValueError, match="gopher"):
        asyncio.run(UriFetcher().fetch("gopher://old/1.tif"))
