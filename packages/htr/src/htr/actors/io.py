"""I/O actors — bridge `storage.Source`/`Sink` to Ray Data columns.

Use these as the first/last `map_batches` calls when you don't want to use
`ray.data.read_binary_files` / `write_*` directly (e.g. when you need
HCP-tuned boto3 config that pyarrow's filesystem doesn't expose)."""

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError


_log = logging.getLogger(__name__)


class PageLoaderActor:
    """First stage. Reads bytes for each `key` from the given source.

    Per-key failures (after the source's own retries) drop the row from
    the output batch. Two ways a key can fail here: (1) ``source.read``
    raises (S3/IIIF unreachable, persistent 5xx, …); (2) the bytes come
    back but PIL refuses to identify them as an image — happens with
    truncated cached JPGs and HTML error pages stashed in S3 from a
    long-ago failed prefetch. Catching both up front prevents the
    downstream GPU stages (Layout, Line, Transcribe) from blowing up
    on bytes they can't decode and killing the whole chunk.

    The skipped keys surface later as the gap between manifest counts
    and transcribed_pages, so they stay visible.

    boto3 is blocking; serial GETs at batch_size=8 cap a single actor
    at ~50 pages/s, so 1 PageLoader keeps 2 Transcribers fed and Ray
    Data's autoscaler never wakes the 3rd GPU. PARALLELISM threads keep
    many GETs in flight per actor — boto3 clients are thread-safe and
    the work is network-bound, so the GIL doesn't bite — lifting the
    ceiling enough that the Transcribe pool fans out across all GPUs.
    """

    PARALLELISM = 16

    def __init__(self, source: Any) -> None:  # noqa: ANN401
        self.source = source
        self._pool = ThreadPoolExecutor(
            max_workers=self.PARALLELISM,
            thread_name_prefix="page-load",
        )

    def _fetch(self, key: str) -> tuple[str, bytes] | None:
        try:
            data = self.source.read(key)
        except Exception as exc:
            _log.warning("PageLoader skipped %s: %s", key, exc)
            return None
        try:
            # verify() walks the headers/structure without fully decoding.
            # Catches both unrecognisable formats and truncation; cheap
            # enough to leave on for every page.
            Image.open(io.BytesIO(data)).verify()
        except (UnidentifiedImageError, OSError, Exception) as exc:
            _log.warning("PageLoader rejected %s (not a valid image): %s", key, exc)
            return None
        return (key, data)

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        results = [r for r in self._pool.map(self._fetch, batch["key"]) if r is not None]
        keys = [r[0] for r in results]
        bytes_list = [r[1] for r in results]
        return {
            "key": np.array(keys, dtype=object),
            "image_bytes": np.array(bytes_list, dtype=object),
        }


class PrefetchActor:
    """Cache-warmer. Reads each ``key`` only for its write-through side effect.

    Used by the ``prefetch`` pipeline: pair with `IIIFCachedSource`, whose
    ``read(k)`` populates the S3 cache on miss. The bytes are deliberately not
    forwarded downstream — that would push image data through Ray's plasma
    store for no reason. Returns just the key column so Ray Data can still
    count rows for progress.

    Per-key failures (after the source's own retries are exhausted) are
    logged and skipped rather than raised. Prefetch is a precondition, not
    the work itself; a single dead IIIF page must not kill the warmup of the
    other ~7,000 pages in the chunk. The keys we couldn't fetch will be
    visible later as the gap between manifest counts and ``cached_pages``.
    """

    def __init__(self, source: Any) -> None:  # noqa: ANN401
        self.source = source

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        for k in batch["key"]:
            try:
                self.source.read(k)
            except Exception as exc:
                _log.warning("prefetch failed for %s: %s", k, exc)
        return {"key": batch["key"]}


class AltoWriterActor:
    """Last stage. Writes each `(output_key, alto_xml)` pair to the given sink."""

    def __init__(self, sink: Any) -> None:  # noqa: ANN401
        self.sink = sink

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        for key, data in zip(batch["output_key"], batch["alto_xml"], strict=True):
            self.sink.write(key, data)
        return batch
