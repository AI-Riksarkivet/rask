"""Shared helpers for the bulk transfer engine."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from rahcp_tracker import TrackerProtocol, TransferStatus

from rahcp_client.bulk.config import DEFAULT_STREAM_THRESHOLD, TransferStats
from rahcp_client.bulk.protocol import BulkClient, S3Client

log = logging.getLogger(__name__)

_DONE = object()


# ── Models ────────────────────────────────────────────────────────


class Counters(BaseModel):
    """Mutable transfer counters — shared across workers."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ok: int = 0
    skipped: int = 0
    errors: int = 0
    total_bytes: int = 0
    done_keys: set[str] = Field(default_factory=set)
    start_time: float = Field(default_factory=time.monotonic)
    last_report: float = Field(default_factory=time.monotonic)


# ── Filters ───────────────────────────────────────────────────────


def matches_filters(name: str, include: list[str], exclude: list[str]) -> bool:
    """Check if a filename matches include/exclude glob patterns."""
    if exclude and any(fnmatch.fnmatch(name, pat) for pat in exclude):
        return False
    if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
        return False
    return True


# ── Tracker helpers ───────────────────────────────────────────────


def record_result(counters: Counters, result: str, byte_count: int = 0) -> None:
    """Update counters from a single transfer result."""
    if result == "ok":
        counters.ok += 1
        counters.total_bytes += byte_count
    elif result == "skipped":
        counters.skipped += 1
    else:
        counters.errors += 1


def mark_done(
    tracker: TrackerProtocol,
    done_keys: set[str],
    key: str,
    size: int,
    *,
    etag: str | None = None,
    validated: bool = False,
    verified: bool = False,
) -> None:
    """Mark a key as done in both the tracker and the in-memory set."""
    tracker.mark(
        key,
        size,
        TransferStatus.done,
        etag=etag,
        validated=validated,
        verified=verified,
    )
    done_keys.add(key)


def mark_error(
    tracker: TrackerProtocol,
    key: str,
    size: int,
    exc: Exception,
    on_error: Callable[[str, Exception], None] | None,
) -> None:
    """Mark a key as failed and notify the error callback."""
    tracker.mark(key, size, TransferStatus.error, str(exc)[:200])
    log.warning("Transfer failed: %s — %s", key, exc)
    if on_error:
        on_error(key, exc)


# ── Validation ────────────────────────────────────────────────────


async def run_validation(
    validate_fn: Callable[[Path], None] | None,
    tracker: TrackerProtocol,
    key: str,
    path: Path,
    size: int,
    on_error: Callable[[str, Exception], None] | None,
    *,
    phase: str = "validation",
) -> bool:
    """Run a file validation callback.

    Returns True if valid (or no validator configured).
    On failure, marks the error in the tracker and fires the callback.
    """
    if not validate_fn:
        return True
    try:
        await asyncio.to_thread(validate_fn, path)
        return True
    except Exception as exc:
        tracker.mark(
            key,
            size,
            TransferStatus.error,
            f"{phase}: {exc!s:.200}",
            validated=False,
        )
        log.warning("%s failed: %s — %s", phase.capitalize(), key, exc)
        if on_error:
            on_error(key, exc)
        return False


# ── Stats ─────────────────────────────────────────────────────────


def maybe_report(
    on_progress: Callable[[TransferStats], None] | None,
    progress_interval: float,
    counters: Counters,
) -> None:
    """Fire progress callback if enough time has passed."""
    now = time.monotonic()
    if on_progress and now - counters.last_report >= progress_interval:
        counters.last_report = now
        on_progress(build_stats(counters))


def build_stats(counters: Counters) -> TransferStats:
    """Create a stats snapshot from the current counters."""
    return TransferStats(
        ok=counters.ok,
        skipped=counters.skipped,
        errors=counters.errors,
        total_bytes=counters.total_bytes,
        elapsed=time.monotonic() - counters.start_time,
    )


# ── Client settings ──────────────────────────────────────────────


def pool_settings(client: BulkClient) -> tuple[bool, float, int]:
    """Derive connection pool settings from client transfer settings.

    Returns ``(verify_ssl, pool_timeout, multipart_threshold)``.
    """
    settings = client.transfer_settings
    pool_timeout = max(120.0, settings.timeout * 2)
    return settings.verify_ssl, pool_timeout, settings.multipart_threshold


def make_pool(
    max_connections: int,
    *,
    verify_ssl: bool = True,
    timeout: float = 120.0,
) -> httpx.AsyncClient:
    """Create a shared connection pool for presigned URL transfers.

    Reuses TCP/TLS connections across workers — avoids handshake overhead
    per file. The pool is sized to match the worker count.
    """
    return httpx.AsyncClient(
        verify=verify_ssl,
        timeout=httpx.Timeout(timeout, connect=10.0),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        ),
    )


# ── Shared I/O ────────────────────────────────────────────────────


def _raise_for_presigned(resp: httpx.Response, bucket: str, key: str) -> None:
    """Raise a clean error for presigned URL failures."""
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {resp.reason_phrase} for {bucket}/{key}",
            request=resp.request,
            response=resp,
        )


async def pool_upload(
    pool: httpx.AsyncClient,
    presigned_url: str,
    file_path: Path,
    bucket: str,
    key: str,
) -> str:
    """Upload a file using a presigned URL and shared pool. Returns the ETag."""
    content = await asyncio.to_thread(file_path.read_bytes)
    resp = await pool.put(presigned_url, content=content)
    _raise_for_presigned(resp, bucket, key)
    return resp.headers.get("etag", "")


async def pool_download(
    pool: httpx.AsyncClient,
    presigned_url: str,
    dest: Path,
    bucket: str,
    key: str,
    *,
    size: int = 0,
    chunk_size: int = 256 * 1024,
    stream_threshold: int = DEFAULT_STREAM_THRESHOLD,
) -> int:
    """Download a file using a presigned URL and shared pool.

    Small files (< stream_threshold) are read in one shot.
    Large files are streamed chunk-by-chunk.
    Returns the number of bytes downloaded.
    """
    if 0 < size <= stream_threshold:
        resp = await pool.get(presigned_url)
        _raise_for_presigned(resp, bucket, key)
        await asyncio.to_thread(dest.write_bytes, resp.content)
        return len(resp.content)

    async with pool.stream("GET", presigned_url) as resp:
        _raise_for_presigned(resp, bucket, key)
        total = 0
        fh = await asyncio.to_thread(open, dest, "wb")
        try:
            async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                await asyncio.to_thread(fh.write, chunk)
                total += len(chunk)
        finally:
            await asyncio.to_thread(fh.close)
    return total


# ── Pagination ────────────────────────────────────────────────────


async def paginate_objects(
    s3: S3Client,
    bucket: str,
    prefix: str,
    *,
    max_keys: int = 1000,
) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over all objects in a bucket prefix."""
    token: str | None = None
    while True:
        data = await s3.list_objects(
            bucket, prefix, max_keys=max_keys, continuation_token=token
        )
        for obj in data.get("objects", []):
            yield obj
        if not data.get("is_truncated"):
            return
        token = data.get("next_continuation_token")


# ── Pipeline ──────────────────────────────────────────────────────


async def run_pipeline(
    *,
    workers: int,
    queue_depth: int,
    pool: httpx.AsyncClient,
    counters: Counters,
    tracker: TrackerProtocol,
    on_progress: Callable[[TransferStats], None] | None,
    progress_interval: float,
    transfer_fn: Callable[..., Awaitable[tuple[str, int]]],
    produce_fn: Callable[[asyncio.Queue], Awaitable[None]],
) -> TransferStats:
    """Run a producer-consumer transfer pipeline.

    ``produce_fn`` pushes work items onto the queue.
    ``transfer_fn`` processes each item (unpacked from the tuple) and
    returns ``(result_label, byte_count)``.
    Worker lifecycle and sentinel values are managed here.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=workers * queue_depth)

    async def _worker() -> None:
        while True:
            item = await queue.get()
            if item is _DONE:
                queue.task_done()
                break
            try:
                result, byte_count = await transfer_fn(*item)
                record_result(counters, result, byte_count)
                maybe_report(on_progress, progress_interval, counters)
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(_worker()) for _ in range(workers)]
    try:
        await produce_fn(queue)
    except Exception:
        log.exception("Producer failed")
    finally:
        for _ in range(workers):
            await queue.put(_DONE)

    # Wait for ALL workers to finish before closing the pool
    await asyncio.gather(*tasks, return_exceptions=True)
    await pool.aclose()

    tracker.commit()
    return build_stats(counters)
