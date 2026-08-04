"""Unit fetching — key to bytes, resolved by SCHEME and by nothing else.

A worker receives a `UnitTask`, and a task carries a key, not a source spec. That is deliberate: the
task is the only thing that crosses the queue, and making it carry an adapter's configuration would
put source-specific knowledge back on the wire — the coupling I1 exists to remove.

**This module knows about SCHEMES. It must never know about SOURCES.**

That is not a stylistic preference, it is the plane's reason to exist, and the first version broke
it: `_fetch_http` imported `storage.iiif.fetch_image`, so every `http(s)://` key — from any source,
IIIF or not — inherited one particular source's client and retry policy. The plane that was built to
un-weld IIIF from twelve medallion files had welded it into the one place every HTTP source must
pass through.

Reading `storage.iiif.fetch_image` closely is what settles it: its retry is **entirely generic** —
retry transport errors and 5xx with exponential backoff, fail fast on 4xx except 429. There is
nothing IIIF about that policy; it lives in `iiif.py` only because that is where it was first
needed. So the fix is not to abstract it, it is to implement the generic policy generically and let
the IIIF adapter keep the parts that ARE IIIF: the manifest read and the URL construction, both of
which already live in `ingest/adapters.py` where they belong.

Where per-source behaviour genuinely differs — auth headers, a cache read-through, a bespoke
backoff — it belongs to that source's ADAPTER, which can supply its own `Fetcher`. Nothing about
that reaches this module.

**Blocking calls run in a thread.** Every fetch below is synchronous, and the worker is async with
bounded concurrency. Calling them inline would block the event loop and serialise the fetches that
`FETCH_CONCURRENCY` exists to overlap, turning the concurrency ceiling into a lie that shows up only
as an unexplained throughput cliff.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


#: Retry budget for a transient HTTP failure. Three tries with exponential backoff turns a server
#: hiccup into a slowdown rather than a lost unit — the behaviour measured against a real
#: rate-limited endpoint, which hands out RST under load at ~64 concurrent reads.
HTTP_ATTEMPTS = int(os.getenv("RASK_INGEST_HTTP_ATTEMPTS", "3"))
HTTP_BASE_DELAY = float(os.getenv("RASK_INGEST_HTTP_BASE_DELAY", "1.0"))
HTTP_TIMEOUT = float(os.getenv("RASK_INGEST_HTTP_TIMEOUT", "60"))

#: 429 is deliberately EXCLUDED — it is the one 4xx that means "try again". Everything else in this
#: range is a verdict no retry improves, and retrying it spends requests against a source that has
#: already answered.
_RETRYABLE_4XX = frozenset({408, 425, 429})


class UriFetcher:
    """The default `Fetcher`: resolves a unit key by its URI scheme, with no source knowledge."""

    async def fetch(self, key: str) -> bytes:
        scheme = urlparse(key).scheme
        if scheme in ("http", "https"):
            return await asyncio.to_thread(_fetch_http, key)
        if scheme == "s3":
            return await asyncio.to_thread(_fetch_s3, key)
        if scheme in ("file", ""):
            return await asyncio.to_thread(_fetch_file, key)
        raise ValueError(f"no fetcher for scheme {scheme!r} (unit key {key!r})")


def _fetch_http(url: str) -> bytes:
    """GET one object over HTTP, with a generic transient-failure retry.

    Owns its client. The version this replaces called `storage.iiif.fetch_image(url)` — whose
    `client` is a REQUIRED keyword-only argument — so every HTTP fetch raised
    `TypeError: fetch_image() missing 1 required keyword-only argument: 'client'` before it reached
    the network. The path had never been exercised: only `file://` and `s3://` were ever run, so a
    IIIF ingest would have failed on its first unit.

    Retries transport errors and 5xx; 4xx other than 408/425/429 raise immediately, which is what
    lets the worker park a dead page on the first attempt instead of spending its whole redelivery
    budget discovering the same 404 three times.
    """
    import httpx

    last: Exception | None = None
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for attempt in range(HTTP_ATTEMPTS):
            try:
                response = client.get(url)
                response.raise_for_status()
                return response.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if 400 <= code < 500 and code not in _RETRYABLE_4XX:
                        raise
                last = exc
                if attempt < HTTP_ATTEMPTS - 1:
                    time.sleep(HTTP_BASE_DELAY * (2**attempt))
    assert last is not None
    raise last


def _fetch_s3(uri: str) -> bytes:
    """Through the estate's provider-agnostic client, never boto3 directly — which is what keeps a
    bucket movable between RustFS, MinIO, HCP and AWS by env var rather than by a code change."""
    from storage import s3_client, split_s3_uri

    bucket, key = split_s3_uri(uri)
    client = s3_client(os.getenv("RASK_S3_ENDPOINT_URL"))
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _fetch_file(uri: str) -> bytes:
    """`unquote` is load-bearing: `Path.as_uri()` percent-encodes, so a fixture named `sida 1.tif`
    round-trips as `sida%201.tif` and the read fails with a FileNotFoundError naming a path that
    visibly exists on disk."""
    parsed = urlparse(uri)
    return Path(unquote(parsed.path if parsed.scheme == "file" else uri)).read_bytes()
