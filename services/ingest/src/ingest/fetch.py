"""Unit fetching — key to bytes, resolved by SCHEME and by nothing else.

A worker receives a `UnitTask`, and a task carries a key, not a source spec. That is deliberate: the
task is the only thing that crosses the queue, and making it carry an adapter's configuration would
put source-specific knowledge back on the wire — the coupling I1 exists to remove.

The one thing a task does carry besides the key is `source_endpoint`: WHICH object store an `s3://`
key names. That is not an adapter's configuration — no kind, no option names, no listing rules — it
is the other half of the address, and without it "no source knowledge" collapsed into "the estate's
own store, always", which silently answered an external key from a same-named local bucket.

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
The fetch-concurrency ceiling exists to overlap, turning the concurrency ceiling into a lie that shows up only
as an unexplained throughput cliff.
"""

from __future__ import annotations

import asyncio
import os
import time
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
    """The default `Fetcher`: resolves a unit key by its URI scheme, with no source knowledge.

    `source_endpoint` is the object store the RUN declared, carried on the task. It is CONNECTION
    config for the `s3` scheme, not source knowledge: this module still cannot name a kind, read an
    adapter's options, or tell an S3 prefix from a IIIF volume — it is told which S3, the way it is
    told which URL. Schemes that address no store ignore it.
    """

    async def fetch(self, key: str, *, source_endpoint: str | None = None) -> bytes:
        scheme = urlparse(key).scheme
        if scheme in ("http", "https"):
            return await asyncio.to_thread(_fetch_http, key)
        if scheme == "s3":
            return await asyncio.to_thread(_fetch_s3, key, source_endpoint)
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
                # POLL REASON: BACKOFF, not a poll — this waits between BOUNDED retry attempts of one
                # request (`attempt < HTTP_ATTEMPTS - 1`) and asks for no state on a schedule. It ends
                # by exhausting attempts, never by an answer arriving, which is what makes it
                # categorically different from the loop A13 forbids. Exponential so a rate-limited
                # endpoint gets increasing room instead of a fixed drumbeat.
                if attempt < HTTP_ATTEMPTS - 1:
                    time.sleep(HTTP_BASE_DELAY * (2**attempt))
    assert last is not None
    raise last


def _fetch_s3(uri: str, endpoint: str | None = None) -> bytes:
    """Through the estate's provider-agnostic client, never boto3 directly — which is what keeps a
    bucket movable between RustFS, MinIO, HCP and AWS by env var rather than by a code change.

    **On the endpoint the RUN declared, not the estate's.** This read `RASK_S3_ENDPOINT_URL`
    unconditionally, so a run whose source lives on an external store was fetched from the
    deployment's own: at best every unit parked on the DLQ, at worst a bucket of the same name here
    answered and the run ingested the wrong bytes under an external `source_uri` with no error at
    all. `objectstore.resolve_source_connection` refuses an endpoint the storage registry does not
    account for and takes its credentials from the Dapr secret store — never from env, which holds
    the WAREHOUSE's keys.

    `None` keeps the previous behaviour exactly: `storage.s3_client` resolves the endpoint from env
    itself, which is why the explicit `os.getenv` is gone rather than moved.
    """
    from ingest.objectstore import resolve_source_connection, source_s3_client
    from storage import split_s3_uri

    bucket, key = split_s3_uri(uri)
    client = source_s3_client(resolve_source_connection(endpoint, bucket))
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _fetch_file(uri: str) -> bytes:
    """Read a local file, CONFINED to the configured local-dir root.

    The confinement is repeated here rather than trusted from the adapter, because a unit key crosses
    the QUEUE as a bare `file://` URI: whatever enumerated it is long gone by the time a worker reads
    it, and anything able to enqueue would otherwise bypass a check that lives only at enumeration.
    Two checks on the same rule, at the two places the rule can be broken.

    `unquote` is load-bearing: `Path.as_uri()` percent-encodes, so a fixture named `sida 1.tif`
    round-trips as `sida%201.tif` and the read fails with a FileNotFoundError naming a path that
    visibly exists on disk. It runs BEFORE confinement, so an encoded traversal (`%2e%2e%2f`) is
    decoded and then refused rather than slipping past as an opaque string.
    """
    from ingest.adapters import confine_to_local_root

    parsed = urlparse(uri)
    return confine_to_local_root(unquote(parsed.path if parsed.scheme == "file" else uri)).read_bytes()
