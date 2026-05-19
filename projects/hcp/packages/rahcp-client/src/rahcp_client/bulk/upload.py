"""Bulk upload — producer-consumer pipeline with batch presigning."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from rahcp_client.bulk.config import BulkUploadConfig, TransferStats
from rahcp_client.bulk.helpers import (
    Counters,
    make_pool,
    mark_done,
    mark_error,
    matches_filters,
    pool_settings,
    pool_upload,
    run_pipeline,
    run_validation,
)

log = logging.getLogger(__name__)


# ── File scanning (runs in a thread) ─────────────────────────────


def _scan_files(
    src: Path,
    prefix: str,
    include: list[str],
    exclude: list[str],
    done_keys: set[str],
) -> list[tuple[Path, str]]:
    """Walk source directory and build a list of (file_path, object_key) pairs."""
    result: list[tuple[Path, str]] = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if not matches_filters(f.name, include, exclude):
            continue
        rel = f.relative_to(src)
        key = f"{prefix}{rel}" if prefix else str(rel)
        if key not in done_keys:
            result.append((f, key))
    return result


# ── Entry point ───────────────────────────────────────────────────


async def bulk_upload(cfg: BulkUploadConfig) -> TransferStats:
    """Upload a directory to S3 with batch presigning and connection pooling."""
    from rahcp_client.errors import ConflictError, NotFoundError

    src = cfg.source_dir
    counters = Counters(done_keys=cfg.tracker.done_keys())

    verify_ssl, pool_timeout, multipart_threshold = pool_settings(cfg.client)
    pool = make_pool(cfg.workers, verify_ssl=verify_ssl, timeout=pool_timeout)

    log.info(
        "Bulk upload: %s → s3://%s/%s (%d workers, presign batch %d, %d done)",
        src,
        cfg.bucket,
        cfg.prefix,
        cfg.workers,
        cfg.presign_batch_size,
        len(counters.done_keys),
    )

    # ── Guard helpers ────────────────────────────────────────────

    async def _check_remote_exists(key: str, local_size: int) -> bool:
        """HEAD check — True if remote object already matches local size."""
        if not cfg.skip_existing or cfg.retry_errors:
            return False
        try:
            meta = await cfg.client.s3.head(cfg.bucket, key)
            remote_size = meta.get("content-length")
            return remote_size is None or int(remote_size) == local_size
        except NotFoundError:
            return False
        except Exception:
            log.debug("HEAD check failed for %s, proceeding with upload", key)
            return False

    async def _upload_and_verify(
        key: str,
        file_path: Path,
        presigned_url: str | None,
        local_size: int,
        validated: bool,
    ) -> tuple[str, int]:
        """Perform the actual upload and optional post-upload verification."""
        try:
            if presigned_url and local_size < multipart_threshold:
                etag = await pool_upload(
                    pool, presigned_url, file_path, cfg.bucket, key
                )
            else:
                etag = await cfg.client.s3.upload(cfg.bucket, key, file_path)

            verified = False
            if cfg.verify_upload:
                meta = await cfg.client.s3.head(cfg.bucket, key)
                remote_size = int(meta.get("content-length", 0))
                if remote_size != local_size:
                    raise ValueError(
                        f"Size mismatch: local={local_size}, remote={remote_size}"
                    )
                verified = True

            mark_done(
                cfg.tracker,
                counters.done_keys,
                key,
                local_size,
                etag=etag or None,
                validated=validated,
                verified=verified,
            )
            return "ok", local_size

        except ConflictError:
            mark_done(cfg.tracker, counters.done_keys, key, local_size)
            return "skipped", 0
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                mark_done(cfg.tracker, counters.done_keys, key, local_size)
                return "skipped", 0
            mark_error(cfg.tracker, key, local_size, exc, cfg.on_error)
            return "error", 0
        except Exception as exc:
            mark_error(cfg.tracker, key, local_size, exc, cfg.on_error)
            return "error", 0

    # ── Per-file transfer logic ───────────────────────────────────

    async def transfer_one(
        file_path: Path, key: str, presigned_url: str | None
    ) -> tuple[str, int]:
        if key in counters.done_keys:
            return "skipped", 0

        local_size = file_path.stat().st_size

        if not await run_validation(
            cfg.validate_file, cfg.tracker, key, file_path, local_size, cfg.on_error
        ):
            return "error", 0

        if await _check_remote_exists(key, local_size):
            mark_done(
                cfg.tracker,
                counters.done_keys,
                key,
                local_size,
                validated=cfg.validate_file is not None,
            )
            return "skipped", 0

        return await _upload_and_verify(
            key,
            file_path,
            presigned_url,
            local_size,
            validated=cfg.validate_file is not None,
        )

    # ── Producer ──────────────────────────────────────────────────

    async def produce(queue: asyncio.Queue) -> None:
        if cfg.retry_errors:
            for key, _ in cfg.tracker.error_entries():
                await queue.put((src / key, key, None))
            return

        file_list = await asyncio.to_thread(
            _scan_files, src, cfg.prefix, cfg.include, cfg.exclude, counters.done_keys
        )

        pending: list[tuple[Path, str]] = []

        async def flush() -> None:
            if not pending:
                return
            keys = [k for _, k in pending]
            try:
                url_map = await cfg.client.s3.presign_bulk(
                    cfg.bucket, keys, method="put_object"
                )
            except Exception:
                log.warning("Bulk presign failed, falling back to per-file")
                url_map = {}
            for fp, k in pending:
                await queue.put((fp, k, url_map.get(k)))
            pending.clear()

        for fp, key in file_list:
            pending.append((fp, key))
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
