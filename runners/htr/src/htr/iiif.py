"""IIIF Image API client + S3-cached Source for runner.

Vendored from rahcp-iiif (manifest parsing) so we don't pull in the full
rahcp-tracker / async download stack just to fetch a few image bytes.
The Source implementation here uses sync httpx + boto3 because Ray Data
actors call `read(key)` synchronously and we want to keep the wire format
identical to FSSource / S3Source.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from storage.client import s3_client


log = logging.getLogger(__name__)

DEFAULT_IIIF_BASE = "https://iiifintern-ai.ra.se"
DEFAULT_QUERY_PARAMS = "full/max/0/default.jpg"


def get_image_ids(
    batch_id: str,
    *,
    base_url: str = DEFAULT_IIIF_BASE,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> list[str]:
    """Fetch a IIIF manifest and return sorted image IDs.

    Riksarkivet manifest items have ids of the form
    `https://iiifintern-ai.ra.se/arkis!A0060198_00001/canvas`; we keep the
    first 14 chars after the `!` (e.g. `A0060198_00001`).
    """
    manifest_url = f"{base_url}/arkis!{batch_id}/manifest"
    log.info("Fetching manifest: %s", manifest_url)

    if client is None:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(manifest_url)
            resp.raise_for_status()
            data = resp.json()
    else:
        resp = client.get(manifest_url)
        resp.raise_for_status()
        data = resp.json()

    image_ids: list[str] = []
    for item in data.get("items", []):
        raw_id = item.get("id", "")
        if "!" in raw_id:
            image_ids.append(raw_id.split("!")[1][:14].upper())

    log.info("Found %d images in batch %s", len(image_ids), batch_id)
    return sorted(image_ids)


def build_image_url(
    image_id: str,
    *,
    base_url: str = DEFAULT_IIIF_BASE,
    query_params: str = DEFAULT_QUERY_PARAMS,
) -> str:
    """Build a IIIF Image API URL for a given image ID."""
    return f"{base_url}/arkis!{image_id}/{query_params}"


def file_extension(query_params: str = DEFAULT_QUERY_PARAMS) -> str:
    """Extract the file extension implied by the IIIF query params."""
    return "." + query_params.rsplit(".", 1)[-1]


def fetch_image(url: str, *, client: httpx.Client, attempts: int = 3, base_delay: float = 1.0) -> bytes:
    """GET one IIIF image with retry on transient httpx errors and 5xx — the shared reading primitive.

    The IIIF server hands out RST under load (Errno 104 at ~64 concurrent reads); three tries with
    exponential backoff convert a transient hiccup into a slowdown instead of a pipeline kill. Real 4xx
    (except 429) raise immediately — those won't get better. Used by both :class:`IIIFCachedSource` (the
    legacy cache role) and the medallion IIIF→raw producer (the P7a harvest library role).
    """
    import time as _time

    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                if code != 429 and 400 <= code < 500:
                    raise
            last = exc
            if i < attempts - 1:
                _time.sleep(base_delay * (2**i))
    assert last is not None
    raise last


class IIIFCachedSource:
    """Source that reads images from S3, falling back to IIIF on cache miss.

    On miss the bytes are written back to the same S3 bucket so subsequent
    runs (and other actors in the same run) hit the cache. `keys()` lists
    the expected keys by fetching one IIIF manifest per batch_id; `read(key)`
    is the per-page primitive used by `PageLoaderActor`.

    Picklable for Ray actors via lazy clients + __getstate__.
    """

    def __init__(
        self,
        *,
        batch_ids: Iterable[str],
        cache_bucket: str,
        s3_endpoint: str | None = None,
        iiif_base: str = DEFAULT_IIIF_BASE,
        query_params: str = DEFAULT_QUERY_PARAMS,
        # 15s per attempt; with 3 attempts and 1+2s backoff that's ~48s worst
        # case before we give up on a key. The previous 60s default meant a
        # single sticky URL could block an actor for 3 min, freezing prefetch
        # progress whenever IIIF was overloaded (the very moment we want to
        # bail and move on).
        timeout: float = 15.0,
        s3_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.batch_ids = list(batch_ids)
        self.cache_bucket = cache_bucket
        self.s3_endpoint = s3_endpoint
        self.iiif_base = iiif_base.rstrip("/")
        self.query_params = query_params
        self.timeout = timeout
        # Same lazy-factory pattern as S3Source so we can pickle safely.
        self._s3_factory = s3_client_factory or functools.partial(s3_client, s3_endpoint)
        self._s3: Any = None
        self._http: httpx.Client | None = None

    @property
    def s3(self) -> Any:  # noqa: ANN401 — boto3 client has no public stub
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def __getstate__(self) -> dict:
        # Drop live clients before pickle; they get re-created lazily after unpickle.
        state = self.__dict__.copy()
        state["_s3"] = None
        state["_http"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def close(self) -> None:
        """Best-effort close of the http client. boto3 clients clean themselves up."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def keys(self) -> Iterable[str]:
        """Yield `<batch_id>/<image_id><ext>` for every image in every manifest."""
        ext = file_extension(self.query_params)
        for batch in self.batch_ids:
            for image_id in get_image_ids(batch, base_url=self.iiif_base, timeout=self.timeout, client=self.http):
                yield f"{batch}/{image_id}{ext}"

    def cached_keys(self) -> Iterable[str]:
        """Yield keys already present in the S3 cache for this source's batches.

        Mirrors ``Sink.existing_keys`` on the output side — lets the runner
        diff manifest keys against what's already cached before scheduling
        prefetch work.
        """
        paginator = self.s3.get_paginator("list_objects_v2")
        for batch in self.batch_ids:
            for page in paginator.paginate(Bucket=self.cache_bucket, Prefix=f"{batch}/"):
                for obj in page.get("Contents", []):
                    yield obj["Key"]

    def read(self, key: str) -> bytes:
        """Return image bytes — S3 first, IIIF on miss (with write-through)."""
        # 1. Try S3 cache.
        try:
            return self.s3.get_object(Bucket=self.cache_bucket, Key=key)["Body"].read()
        except Exception as exc:
            # boto3 raises ClientError for any non-2xx; we treat anything other
            # than NoSuchKey/404 as a hard failure (let it propagate).
            err = getattr(exc, "response", {}).get("Error", {}) if hasattr(exc, "response") else {}
            if err.get("Code") not in {"NoSuchKey", "404", "NoSuchBucket"}:
                raise

        # 2. Cache miss → fetch from IIIF, with retry/backoff.
        #    The server hands out RST under load (we hit Errno 104 at ~64
        #    concurrent reads). Three tries with exponential backoff
        #    converts a transient hiccup into a slowdown instead of a
        #    pipeline kill — failed retry still propagates.
        # key shape: "<batch>/<image_id>.jpg" → image_id is the stem of the basename.
        image_id = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        url = build_image_url(image_id, base_url=self.iiif_base, query_params=self.query_params)
        log.info("Cache miss for %s — fetching %s", key, url)
        data = self._fetch_with_retry(url)

        # 3. Write-through to S3 so the next reader (or run) hits the cache.
        try:
            self.s3.put_object(Bucket=self.cache_bucket, Key=key, Body=data, ContentType="image/jpeg")
        except Exception as exc:
            # Don't fail the read on a cache write hiccup — log and return bytes.
            log.warning("Cache write failed for %s: %s", key, exc)
        return data

    def _fetch_with_retry(self, url: str, *, attempts: int = 3, base_delay: float = 1.0) -> bytes:
        """GET ``url`` with retry — delegates to the module-level :func:`fetch_image` primitive."""
        return fetch_image(url, client=self.http, attempts=attempts, base_delay=base_delay)
