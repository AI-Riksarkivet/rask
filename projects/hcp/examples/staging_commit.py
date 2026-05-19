#!/usr/bin/env python3
"""Atomic staging → commit pattern for safe batch writes.

Uploads files to a staging/ prefix, then atomically commits them to
the final prefix. If anything fails, the staging prefix is cleaned up.

This pattern ensures downstream consumers never see partial results.
"""

from __future__ import annotations

import asyncio
import json

from rahcp_client import HCPClient

BUCKET = "datasets"
STAGING_PREFIX = "staging/batch-001/"
FINAL_PREFIX = "processed/batch-001/"


async def main() -> None:
    async with HCPClient.from_env() as client:
        # --8<-- [start:staging-commit]
        try:
            # 1. Upload files to a staging prefix
            print("Uploading to staging/...")
            for i in range(5):
                record = {"id": i, "value": f"record-{i}", "status": "processed"}
                data = json.dumps(record).encode()
                key = f"{STAGING_PREFIX}record-{i:03d}.json"
                await client.s3.upload(BUCKET, key, data)
                print(f"  Staged {key}")

            # 2. Verify all files landed
            result = await client.s3.list_objects(BUCKET, prefix=STAGING_PREFIX)
            staged_count = len(result["objects"])
            print(f"\nStaged {staged_count} objects")

            if staged_count != 5:
                raise RuntimeError(f"Expected 5 staged objects, got {staged_count}")

            # 3. Commit: copy staging → final, then delete staging
            print(f"\nCommitting {STAGING_PREFIX} → {FINAL_PREFIX}...")
            committed = await client.s3.commit_staging(
                BUCKET, STAGING_PREFIX, FINAL_PREFIX
            )
            print(f"Committed {committed} objects")

            # 4. Verify final prefix
            result = await client.s3.list_objects(BUCKET, prefix=FINAL_PREFIX)
            print(f"Final prefix contains {len(result['objects'])} objects:")
            for obj in result["objects"]:
                print(f"  - {obj['key']}")

        except Exception:
            # Clean up staging on any failure
            print("\nError — cleaning up staging prefix...")
            deleted = await client.s3.cleanup_staging(BUCKET, STAGING_PREFIX)
            print(f"Deleted {deleted} staged objects")
            raise
        # --8<-- [end:staging-commit]


if __name__ == "__main__":
    asyncio.run(main())
