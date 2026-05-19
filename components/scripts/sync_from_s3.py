"""Reconcile .cache/batches.db with the actual S3 buckets.

For each batch:
  cached_pages       = # of *.jpg under  images-batch/<batch_id>/
  transcribed_pages  = # of *.xml under  images-batch-alto/<batch_id>/
  htr_status         = derived state (see classify())
  started_at, finished_at  = stamped on first/last observation

Lists each bucket once and groups by batch prefix in Python — much
cheaper than 1633 per-prefix lists.

Reads MinIO/HCP creds from .env at the repo root:
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, HCP_ENDPOINT
"""

import argparse
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv


REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / ".cache" / "batches.db"
DEFAULT_CACHE_BUCKET = "images-batch"
DEFAULT_OUTPUT_BUCKET = "images-batch-alto"


def s3_client(endpoint: str | None) -> object:
    return boto3.client("s3", endpoint_url=endpoint, config=Config(s3={"addressing_style": "path"}))


def count_per_batch(client: object, bucket: str, suffix: str) -> dict[str, int]:
    """Return {batch_id: count} for keys '<batch_id>/<file><suffix>'."""
    counts: dict[str, int] = defaultdict(int)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(suffix):
                continue
            batch_id, _, _ = key.partition("/")
            if batch_id:
                counts[batch_id] += 1
    return dict(counts)


def classify(expected: int | None, cached: int, transcribed: int) -> str:
    if expected is None:
        if transcribed > 0:
            return "partial"
        if cached > 0:
            return "cached"
        return "pending"
    if transcribed >= expected:
        return "done"
    if transcribed > 0:
        return "partial"
    if cached >= expected:
        return "cached"
    if cached > 0:
        return "partial"
    return "pending"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-bucket", default=DEFAULT_CACHE_BUCKET)
    parser.add_argument("--output-bucket", default=DEFAULT_OUTPUT_BUCKET)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    load_dotenv(REPO / ".env")
    endpoint = os.environ.get("HCP_ENDPOINT")
    if endpoint is None:
        raise SystemExit(f"HCP_ENDPOINT missing — load {REPO}/.env first")

    print(f"S3 endpoint: {endpoint}")
    client = s3_client(endpoint)

    print(f"Listing {args.cache_bucket}/ ...", flush=True)
    cached = count_per_batch(client, args.cache_bucket, ".jpg")
    print(f"  {sum(cached.values()):,} jpgs across {len(cached)} batches")

    print(f"Listing {args.output_bucket}/ ...", flush=True)
    transcribed = count_per_batch(client, args.output_bucket, ".xml")
    print(f"  {sum(transcribed.values()):,} xmls across {len(transcribed)} batches")

    now = datetime.now(UTC).isoformat(timespec="seconds")
    db = sqlite3.connect(args.db)
    cur = db.execute("SELECT batch_id, page_count, started_at, finished_at FROM batches")
    updates = []
    for batch_id, expected, started_at, finished_at in cur:
        c = cached.get(batch_id, 0)
        t = transcribed.get(batch_id, 0)
        status = classify(expected, c, t)
        new_started = started_at or (now if c > 0 else None)
        new_finished = finished_at or (now if status == "done" else None)
        updates.append((c, t, status, new_started, new_finished, now, batch_id))

    db.executemany(
        """UPDATE batches
              SET cached_pages = ?,
                  transcribed_pages = ?,
                  htr_status = ?,
                  started_at = ?,
                  finished_at = ?,
                  last_synced_at = ?
            WHERE batch_id = ?""",
        updates,
    )
    db.commit()
    print(f"\nUpdated {len(updates)} rows.")

    print("\nHTR status breakdown:")
    print(f"  {'status':<22}{'batches':>10}{'cached':>14}{'transcribed':>14}")
    for status, n, c, t in db.execute("SELECT htr_status, COUNT(*), SUM(cached_pages), SUM(transcribed_pages) FROM batches GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {status:<22}{n:>10}{(c or 0):>14,}{(t or 0):>14,}")

    cur = db.execute("SELECT SUM(page_count), SUM(cached_pages), SUM(transcribed_pages) FROM batches WHERE manifest_status='ok'")
    expected_total, cached_total, trans_total = next(cur)
    if expected_total:
        print("\nProgress on accessible batches:")
        print(f"  pages expected:    {expected_total:,}")
        print(f"  pages cached:      {(cached_total or 0):,}  ({(cached_total or 0) / expected_total * 100:.2f}%)")
        print(f"  pages transcribed: {(trans_total or 0):,}  ({(trans_total or 0) / expected_total * 100:.2f}%)")
    db.close()


if __name__ == "__main__":
    main()
