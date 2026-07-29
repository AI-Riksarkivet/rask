"""Bulk-download Riksarkivet audio/video media from a ``video_batcher`` CSV.

The CSV is semicolon-separated with columns::

    referenskod;namn;extraid;bildid

For each row we fetch ``https://iiifintern-ai.ra.se/api/audiovideo/{bildid}.mp4``
into ``<output_dir>/{bildid}.mp4``. Files that already exist (with non-zero
size) are skipped, so the command is resumable. Downloads happen concurrently
with a bounded pool to stay polite.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

import httpx
from tqdm import tqdm

from ratch.errors import RatchError


logger = logging.getLogger(__name__)

MEDIA_URL = "https://iiifintern-ai.ra.se/api/audiovideo/{bildid}.mp4"


def read_manifest(csv_path: Path) -> list[dict[str, str]]:
    """Parse the semicolon-separated CSV into a list of row dicts."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader if row.get("bildid")]
    return rows


async def _download_one(
    client: httpx.AsyncClient,
    row: dict[str, str],
    *,
    output_dir: Path,
    sem: asyncio.Semaphore,
    pbar: tqdm,
) -> tuple[str, str]:
    """Download a single file. Returns ``(bildid, status)``."""
    bildid = row["bildid"]
    url = MEDIA_URL.format(bildid=bildid)
    dest = output_dir / f"{bildid}.mp4"
    tmp = dest.with_suffix(".mp4.part")

    if dest.exists() and dest.stat().st_size > 0:
        pbar.update(1)
        return bildid, "skipped"

    async with sem:
        try:
            async with client.stream("GET", url, follow_redirects=True) as r:
                if r.status_code != 200:
                    pbar.update(1)
                    return bildid, f"http {r.status_code}"
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
            tmp.replace(dest)
            pbar.update(1)
            return bildid, "ok"
        except Exception as e:
            if tmp.exists():
                tmp.unlink()
            pbar.update(1)
            return bildid, f"error: {e.__class__.__name__}"


async def _run(
    rows: list[dict[str, str]],
    *,
    output_dir: Path,
    concurrency: int,
    timeout: float,
) -> dict[str, list[str]]:
    """Run all downloads concurrently. Returns ``{status: [bildid, ...]}``."""
    sem = asyncio.Semaphore(concurrency)
    buckets: dict[str, list[str]] = {}

    timeout_cfg = httpx.Timeout(timeout, connect=30.0)
    limits = httpx.Limits(max_keepalive_connections=concurrency, max_connections=concurrency)

    async with httpx.AsyncClient(timeout=timeout_cfg, limits=limits) as client:
        with tqdm(total=len(rows), desc="download", unit="file") as pbar:
            tasks = [_download_one(client, row, output_dir=output_dir, sem=sem, pbar=pbar) for row in rows]
            for coro in asyncio.as_completed(tasks):
                bildid, status = await coro
                buckets.setdefault(status, []).append(bildid)
    return buckets


def download_manifest(
    *,
    csv_path: Path,
    output_dir: Path,
    limit: int | None = None,
    concurrency: int = 4,
    timeout: float = 600.0,
) -> dict[str, list[str]]:
    """Download every media file listed in ``csv_path`` into ``output_dir``.

    Args:
        csv_path: The ``video_batcher.csv`` file.
        output_dir: Destination directory. Created if missing. Each file lands
            as ``<output_dir>/{bildid}.mp4``.
        limit: Only download the first N rows (useful for testing).
        concurrency: Number of simultaneous downloads (keep modest — these are
            gigabyte files from a single server).
        timeout: Per-request total timeout in seconds.

    Returns:
        Mapping of status → list of ``bildid`` — e.g.
        ``{"ok": [...], "skipped": [...], "http 404": [...], "error: ReadTimeout": [...]}``.
    """
    if not csv_path.is_file():
        raise RatchError(f"CSV not found: {csv_path}")

    rows = read_manifest(csv_path)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        logger.warning("no rows with `bildid` in %s; nothing to do", csv_path)
        return {}

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "downloading %d file(s) from %s → %s/ (concurrency=%d)",
        len(rows),
        csv_path.name,
        output_dir,
        concurrency,
    )

    buckets = asyncio.run(_run(rows, output_dir=output_dir, concurrency=concurrency, timeout=timeout))

    logger.info("summary:")
    for status in sorted(buckets):
        logger.info("  %-20s %5s", status, len(buckets[status]))
    return buckets
