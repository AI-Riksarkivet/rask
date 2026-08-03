"""Unit fetching — key to bytes, resolved by SCHEME rather than by source kind.

A worker receives a `UnitTask`, and a task carries a key, not a source spec. That is deliberate:
the task is the only thing that crosses the queue, and making it carry an adapter's whole
configuration would put source-specific knowledge back on the wire — the coupling I1 exists to
remove.

So the fetcher dispatches on the key's own scheme. `file://` reads a path, `s3://` reads an object
through the estate's provider-agnostic client, `http(s)://` goes through `storage.iiif`'s retrying
fetch. A new source kind that produces one of those schemes needs nothing here at all, which is the
property that makes I1's "one adapter, one registry entry" claim survive contact with the worker.

**Blocking calls run in a thread.** Every fetch below is synchronous — boto3, requests, pathlib —
and the worker is async with bounded concurrency. Calling them directly would block the event loop
and serialise the fetches that `FETCH_CONCURRENCY` exists to overlap, turning the concurrency
ceiling into a lie that only shows up as an unexplained throughput cliff.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import unquote, urlparse


class UriFetcher:
    """The default `Fetcher`: resolves a unit key by its URI scheme."""

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
    """Through `storage.iiif`, whose retry behaviour against a rate-limited endpoint is hard-won.

    Re-deriving it with a bare httpx call would lose that and look identical until the source starts
    throttling — the medallion's defect was sequential fetching in the request path, never its
    retries, so the retries are the part worth keeping.
    """
    from storage.iiif import fetch_image

    return fetch_image(url)


def _fetch_s3(uri: str) -> bytes:
    from storage import s3_client, split_s3_uri

    bucket, key = split_s3_uri(uri)
    client = s3_client(os.getenv("RASK_S3_ENDPOINT_URL"))
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _fetch_file(uri: str) -> bytes:
    """`unquote` is load-bearing: `Path.as_uri()` percent-encodes, so a fixture named `sida 1.tif`
    round-trips to `sida%201.tif` and the read fails with a FileNotFoundError naming a path that
    visibly exists on disk."""
    parsed = urlparse(uri)
    return Path(unquote(parsed.path if parsed.scheme == "file" else uri)).read_bytes()
