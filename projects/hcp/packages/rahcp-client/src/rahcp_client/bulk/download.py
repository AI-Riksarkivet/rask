"""Bulk download — producer-consumer pipeline with batch presigning."""

from __future__ import annotations

import asyncio
import logging

from rahcp_client.bulk.config import BulkDownloadConfig, TransferStats
from rahcp_client.bulk.helpers import (
    Counters,
    make_pool,
    mark_done,
    mark_error,
    matches_filters,
    paginate_objects,
    pool_download,
    pool_settings,
    run_pipeline,
    run_validation,
)

log = logging.getLogger(__name__)


async def bulk_download(cfg: BulkDownloadConfig) -> TransferStats:
    """Download objects from S3 with batch presigning and connection pooling."""
    dest = cfg.dest_dir
    dest.mkdir(parents=True, exist_ok=True)
    counters = Counters(done_keys=cfg.tracker.done_keys())

    verify_ssl, pool_timeout, _ = pool_settings(cfg.client)
    pool = make_pool(cfg.workers, verify_ssl=verify_ssl, timeout=pool_timeout)

    log.info(
        "Bulk download: s3://%s/%s → %s (%d workers, presign batch %d, %d done)",
        cfg.bucket,
        cfg.prefix,
        dest,
        cfg.workers,
        cfg.presign_batch_size,
        len(counters.done_keys),
    )

    # ── Per-file transfer logic ───────────────────────────────────

    async def transfer_one(
        key: str, size: int, presigned_url: str | None
    ) -> tuple[str, int]:
        if key in counters.done_keys:
            return "skipped", 0

        file_path = dest / key
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Guard: skip if already downloaded and size matches
        if file_path.exists() and file_path.stat().st_size == size:
            mark_done(cfg.tracker, counters.done_keys, key, size)
            return "skipped", 0

        try:
            if presigned_url:
                downloaded_bytes = await pool_download(
                    pool,
                    presigned_url,
                    tmp_path,
                    cfg.bucket,
                    key,
                    size=size,
                    chunk_size=cfg.chunk_size,
                    stream_threshold=cfg.stream_threshold,
                )
            else:
                downloaded_bytes = await cfg.client.s3.download(
                    cfg.bucket, key, tmp_path
                )

            if cfg.verify_download and tmp_path.stat().st_size != size:
                tmp_path.unlink(missing_ok=True)
                raise ValueError(
                    f"Size mismatch: expected={size}, got={tmp_path.stat().st_size}"
                )
            tmp_path.rename(file_path)

        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            mark_error(cfg.tracker, key, size, exc, cfg.on_error)
            return "error", 0

        # Post-download validation
        if not await run_validation(
            cfg.validate_file, cfg.tracker, key, file_path, size, cfg.on_error
        ):
            file_path.unlink(missing_ok=True)
            return "error", 0

        mark_done(
            cfg.tracker,
            counters.done_keys,
            key,
            size,
            validated=cfg.validate_file is not None,
        )
        return "ok", downloaded_bytes

    # ── Producer ──────────────────────────────────────────────────

    async def produce(queue: asyncio.Queue) -> None:
        if cfg.retry_errors:
            for key, size in cfg.tracker.error_entries():
                await queue.put((key, size, None))
            return

        pending: list[tuple[str, int]] = []

        async def flush() -> None:
            if not pending:
                return
            keys = [k for k, _ in pending]
            try:
                url_map = await cfg.client.s3.presign_bulk(
                    cfg.bucket, keys, method="get_object"
                )
            except Exception:
                log.warning("Bulk presign failed, falling back to per-file")
                url_map = {}
            for k, sz in pending:
                await queue.put((k, sz, url_map.get(k)))
            pending.clear()

        async for obj in paginate_objects(cfg.client.s3, cfg.bucket, cfg.prefix):
            key = obj["Key"]
            if key.endswith("/") and obj.get("Size", 0) == 0:
                continue
            filename = key.rsplit("/", 1)[-1]
            if not matches_filters(filename, cfg.include, cfg.exclude):
                continue
            if key in counters.done_keys:
                continue
            pending.append((key, obj.get("Size", 0)))
            if len(pending) >= cfg.presign_batch_size:
                await flush()
        await flush()

    # ── Run ───────────────────────────────────────────────────────

    return await run_pipeline(
        workers=cfg.workers,
        queue_depth=cfg.queue_depth,
        pool=pool,
        counters=counters,
        tracker=cfg.tracker,
        on_progress=cfg.on_progress,
        progress_interval=cfg.progress_interval,
        transfer_fn=transfer_one,
        produce_fn=produce,
    )
