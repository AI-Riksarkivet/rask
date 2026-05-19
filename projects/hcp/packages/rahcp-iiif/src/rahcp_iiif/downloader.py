"""Async bulk IIIF image downloader with tracker-based resumability."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from rahcp_tracker import TrackerProtocol, TransferStatus

from rahcp_iiif.manifest import build_image_url, file_extension, get_image_ids

log = logging.getLogger(__name__)

_DONE = object()


class DownloadStats:
    """Running counters for a bulk IIIF download."""

    def __init__(self) -> None:
        self.ok: int = 0
        self.skipped: int = 0
        self.errors: int = 0
        self.total_bytes: int = 0
        self._t0: float = time.monotonic()

    @property
    def done(self) -> int:
        return self.ok + self.skipped + self.errors

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    @property
    def mb_per_sec(self) -> float:
        elapsed = self.elapsed
        return (self.total_bytes / 1024 / 1024) / elapsed if elapsed > 0 else 0.0


async def download_batch(
    batch_id: str,
    output_dir: Path,
    tracker: TrackerProtocol,
    *,
    base_url: str = "https://iiifintern-ai.ra.se",
    query_params: str = "full/max/0/default.jpg",
    timeout: float = 60.0,
    workers: int = 4,
    max_images: int | None = None,
    validate_file: Callable[[Path], None] | None = None,
    on_progress: Callable[[DownloadStats], None] | None = None,
    on_error: Callable[[str, Exception], None] | None = None,
    progress_interval: float = 5.0,
) -> DownloadStats:
    """Download all images from a IIIF batch with parallel workers.

    Args:
        batch_id: Volume/batch identifier (e.g. "C0074667").
        output_dir: Local directory to save images into.
        tracker: Transfer tracker for resumability.
        base_url: IIIF server base URL.
        query_params: IIIF image API parameters (region/size/rotation/quality.format).
        timeout: HTTP request timeout per image.
        workers: Number of concurrent download workers.
        max_images: Limit number of images to download (None = all).
        validate_file: Optional callback to validate downloaded files.
        on_progress: Optional callback for periodic progress updates.
        on_error: Optional callback for per-file errors.
        progress_interval: Minimum seconds between progress callbacks.

    Returns:
        Download statistics.
    """
    image_ids = await get_image_ids(batch_id, base_url=base_url, timeout=timeout)
    if max_images:
        image_ids = image_ids[:max_images]

    ext = file_extension(query_params)
    batch_dir = output_dir / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    done_keys = tracker.done_keys()
    stats = DownloadStats()
    last_report = time.monotonic()
    queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=workers * 8)

    log.info(
        "Downloading batch %s: %d images, %d already done, %d workers",
        batch_id,
        len(image_ids),
        len(done_keys),
        workers,
    )

    async def download_one(client: httpx.AsyncClient, image_id: str) -> None:
        """Download and optionally validate a single image."""
        key = f"{batch_id}/{image_id}{ext}"

        if key in done_keys:
            stats.skipped += 1
            return

        dest = batch_dir / f"{image_id}{ext}"
        url = build_image_url(image_id, base_url=base_url, query_params=query_params)

        try:
            resp = await client.get(url)
            resp.raise_for_status()

            data = resp.content
            await asyncio.to_thread(dest.write_bytes, data)
            file_size = len(data)

            # Post-download validation
            validated = False
            if validate_file:
                try:
                    await asyncio.to_thread(validate_file, dest)
                    validated = True
                except Exception as exc:
                    await asyncio.to_thread(dest.unlink, True)
                    tracker.mark(
                        key,
                        file_size,
                        TransferStatus.error,
                        f"validation: {exc!s}"[:200],
                    )
                    log.warning("Validation failed: %s — %s", key, exc)
                    if on_error:
                        on_error(key, exc)
                    stats.errors += 1
                    return

            tracker.mark(
                key,
                file_size,
                TransferStatus.done,
                validated=validated,
            )
            stats.ok += 1
            stats.total_bytes += file_size

        except Exception as exc:
            tracker.mark(key, 0, TransferStatus.error, str(exc)[:200])
            log.warning("Download failed: %s — %s", key, exc)
            if on_error:
                on_error(key, exc)
            stats.errors += 1

    async def worker(client: httpx.AsyncClient) -> None:
        while True:
            item = await queue.get()
            if item is _DONE:
                queue.task_done()
                break
            try:
                await download_one(client, item)  # type: ignore[arg-type]
                nonlocal last_report
                now = time.monotonic()
                if on_progress and now - last_report >= progress_interval:
                    last_report = now
                    on_progress(stats)
            finally:
                queue.task_done()

    async def produce() -> None:
        for image_id in image_ids:
            await queue.put(image_id)
        for _ in range(workers):
            await queue.put(_DONE)

    async with httpx.AsyncClient(timeout=timeout) as client:
        worker_tasks = [asyncio.create_task(worker(client)) for _ in range(workers)]
        await asyncio.create_task(produce())
        await asyncio.gather(*worker_tasks)

    tracker.commit()
    return stats


async def download_batches(
    batch_ids: list[str],
    output_dir: Path,
    tracker: TrackerProtocol,
    **kwargs,
) -> DownloadStats:
    """Download multiple batches sequentially, sharing one tracker.

    Args:
        batch_ids: List of batch identifiers.
        output_dir: Root output directory.
        tracker: Shared transfer tracker.
        **kwargs: Forwarded to :func:`download_batch`.

    Returns:
        Aggregated download statistics.
    """
    combined = DownloadStats()

    for batch_id in batch_ids:
        batch_id = batch_id.strip()
        if not batch_id:
            continue

        stats = await download_batch(
            batch_id,
            output_dir,
            tracker,
            **kwargs,
        )
        combined.ok += stats.ok
        combined.skipped += stats.skipped
        combined.errors += stats.errors
        combined.total_bytes += stats.total_bytes

    tracker.commit()
    return combined
