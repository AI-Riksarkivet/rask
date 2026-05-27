"""CLI wrapper around `control.reconcile_from_s3`.

Kept so `scripts/orchestrator.py` (which subprocess-invokes this) and the
Makefile keep working unchanged. New code should call `control` directly
instead of shelling out to this script.
"""

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from control import reconcile_from_s3


REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / ".cache" / "batches.db"


async def _run(db_path: str, hcp_endpoint: str, cache_bucket: str, output_bucket: str) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await reconcile_from_s3(
                session,
                hcp_endpoint=hcp_endpoint,
                cache_bucket=cache_bucket,
                output_bucket=output_bucket,
            )
    finally:
        await engine.dispose()

    print(f"Updated {result.rows_updated} rows.")
    print(f"  cached jpgs:      {result.cached_total:,}")
    print(f"  transcribed xmls: {result.transcribed_total:,}")
    print("  by htr_status:")
    for status, n in sorted(result.by_status.items()):
        print(f"    {status:<18} {n:>5}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-bucket", default="images-batch")
    parser.add_argument("--output-bucket", default="images-batch-alto")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    load_dotenv(REPO / ".env")
    endpoint = os.environ.get("HCP_ENDPOINT")
    if endpoint is None:
        raise SystemExit(f"HCP_ENDPOINT missing — load {REPO}/.env first")

    asyncio.run(_run(args.db, endpoint, args.cache_bucket, args.output_bucket))


if __name__ == "__main__":
    main()
