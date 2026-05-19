"""Fetch IIIF manifests in parallel from lbiiif.riksarkivet.se and report page counts.

Usage:
    uv run python /tmp/count_manifests.py < /tmp/batch_ids.txt
Output:
    /tmp/manifest-counts.csv  (batch_id, pages, error)
    Summary on stdout.
"""

import asyncio
import csv
import sys
import time
from pathlib import Path

import httpx


BASE_URL = "https://iiifintern-ai.ra.se"
CONCURRENCY = 30
TIMEOUT = 30.0
RETRIES = 2


def parse_count(data: dict) -> int:
    return sum(1 for it in data.get("items", []) if "!" in it.get("id", ""))


async def fetch(client: httpx.AsyncClient, batch: str, sem: asyncio.Semaphore) -> tuple[str, int | None, str | None]:
    url = f"{BASE_URL}/arkis!{batch}/manifest"
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            async with sem:
                resp = await client.get(url)
            if resp.status_code == 200:
                return batch, parse_count(resp.json()), None
            last_err = f"HTTP {resp.status_code}"
            if 500 <= resp.status_code < 600 and attempt < RETRIES:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return batch, None, last_err
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = type(e).__name__
            if attempt < RETRIES:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return batch, None, last_err
    return batch, None, last_err


async def main() -> None:
    batches = [line.strip() for line in sys.stdin if line.strip()]
    print(f"Fetching {len(batches)} manifests with concurrency={CONCURRENCY}...", file=sys.stderr)

    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT, http2=False) as client:
        tasks = [fetch(client, b, sem) for b in batches]
        # Stream results as they complete to make progress visible.
        results: list[tuple[str, int | None, str | None]] = []
        for done, coro in enumerate(asyncio.as_completed(tasks), start=1):
            batch, count, err = await coro
            results.append((batch, count, err))
            if done % 50 == 0 or done == len(batches):
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed else 0
                print(f"  {done}/{len(batches)} ({rate:.1f}/s)", file=sys.stderr)

    elapsed = time.monotonic() - t0
    repo = Path(__file__).resolve().parents[1]
    out = repo / ".cache" / "manifest-counts.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["batch_id", "pages", "error"])
        for batch, count, err in sorted(results):
            w.writerow([batch, count if count is not None else "", err or ""])

    counts = [c for _, c, e in results if c is not None and e is None]
    errors = [(b, e) for b, c, e in results if e is not None]
    total = sum(counts)
    counts_sorted = sorted(counts)

    def pct(p: float) -> int:
        return counts_sorted[int(len(counts_sorted) * p)] if counts_sorted else 0

    print(f"\nFetched in {elapsed:.1f}s")
    print(f"Successful: {len(counts)}/{len(results)}")
    print(f"Errors:     {len(errors)}")
    if errors[:5]:
        print(f"  sample:   {errors[:5]}")
    print(f"Total pages:    {total:,}")
    if counts:
        print(f"  per-batch min:    {min(counts)}")
        print(f"  per-batch p50:    {pct(0.50)}")
        print(f"  per-batch p90:    {pct(0.90)}")
        print(f"  per-batch p95:    {pct(0.95)}")
        print(f"  per-batch max:    {max(counts)}")
        print(f"  per-batch mean:   {total / len(counts):.1f}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
