#!/usr/bin/env python3
"""Resumable bulk upload with SQLite progress tracking.

Usage:
    python bulk_upload.py /path/to/local/files my-bucket [--prefix 2025/] [--workers 20]

Re-run the same command to resume — already-uploaded files are skipped automatically.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rahcp_client import BulkUploadConfig, HCPClient, TransferTracker, bulk_upload


# --8<-- [start:bulk-upload]
async def run_bulk_upload(
    source_dir: Path,
    bucket: str,
    prefix: str = "",
    workers: int = 10,
    retry_errors: bool = False,
) -> None:
    """Upload a directory to HCP S3 with resumable progress tracking."""
    tracker = TransferTracker(Path(".upload-tracker.db"), flush_every=200)

    async with HCPClient.from_env() as client:
        stats = await bulk_upload(
            BulkUploadConfig(
                client=client,
                bucket=bucket,
                source_dir=source_dir,
                tracker=tracker,
                prefix=prefix,
                workers=workers,
                skip_existing=True,
                retry_errors=retry_errors,
                on_progress=lambda s: print(
                    f"  {s.done} files ({s.ok} ok, {s.skipped} skipped) | "
                    f"{s.mb_per_sec:.1f} MB/s | "
                    f"{s.errors} errors"
                ),
                on_error=lambda key, exc: print(f"  FAILED: {key} — {exc}"),
            )
        )

    print()
    print(f"Done: {stats.ok} uploaded, {stats.skipped} skipped, {stats.errors} errors")
    print(f"Throughput: {stats.mb_per_sec:.1f} MB/s over {stats.elapsed:.0f}s")
    print(f"Tracker DB: {tracker.summary()}")
    tracker.close()


# --8<-- [end:bulk-upload]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk upload a directory to HCP S3")
    parser.add_argument("source_dir", type=Path, help="Local directory to upload")
    parser.add_argument("bucket", help="Target S3 bucket")
    parser.add_argument("--prefix", default="", help="Key prefix (e.g. '2025/')")
    parser.add_argument("--workers", type=int, default=10, help="Concurrent uploads")
    parser.add_argument(
        "--retry-errors", action="store_true", help="Only retry previously failed files"
    )
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        raise SystemExit(f"Not a directory: {args.source_dir}")

    await run_bulk_upload(
        source_dir=args.source_dir,
        bucket=args.bucket,
        prefix=args.prefix,
        workers=args.workers,
        retry_errors=args.retry_errors,
    )


if __name__ == "__main__":
    asyncio.run(main())
