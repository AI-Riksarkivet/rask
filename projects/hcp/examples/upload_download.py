#!/usr/bin/env python3
"""Basic S3 operations: upload, download, list, and delete.

Requires HCP_ENDPOINT, HCP_USERNAME, HCP_PASSWORD env vars (or a config file).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rahcp_client import HCPClient

BUCKET = "datasets"


async def main() -> None:
    async with HCPClient.from_env() as client:
        # --8<-- [start:upload]
        # Upload a file (auto-selects presigned PUT or multipart based on size)
        payload = {"id": 1, "status": "processed", "tags": ["scan", "2025"]}
        local = Path("/tmp/example-record.json")
        local.write_text(json.dumps(payload))

        etag = await client.s3.upload(BUCKET, "examples/record.json", local)
        print(f"Uploaded  → etag={etag}")
        # --8<-- [end:upload]

        # --8<-- [start:download]
        # Download to bytes
        data = await client.s3.download_bytes(BUCKET, "examples/record.json")
        print(f"Downloaded {len(data)} bytes: {json.loads(data)}")

        # Download to file
        dest = Path("/tmp/downloaded-record.json")
        size = await client.s3.download(BUCKET, "examples/record.json", dest)
        print(f"Saved {size} bytes to {dest}")
        # --8<-- [end:download]

        # --8<-- [start:head]
        # Object metadata (HEAD) — returns raw HTTP response headers
        meta = await client.s3.head(BUCKET, "examples/record.json")
        print(
            f"Metadata  → content-length={meta.get('content-length')}, etag={meta.get('etag')}"
        )
        # --8<-- [end:head]

        # --8<-- [start:list]
        # List objects under a prefix
        result = await client.s3.list_objects(BUCKET, prefix="examples/", max_keys=10)
        print(f"Objects   → {[obj['key'] for obj in result['objects']]}")
        # --8<-- [end:list]

        # --8<-- [start:presign]
        # Generate presigned URLs for sharing or browser-based transfers
        get_url = await client.s3.presign_get(
            BUCKET, "examples/record.json", expires=3600
        )
        put_url = await client.s3.presign_put(
            BUCKET, "examples/upload-here.json", expires=3600
        )
        print(f"GET URL   → {get_url[:80]}...")
        print(f"PUT URL   → {put_url[:80]}...")
        # --8<-- [end:presign]

        # --8<-- [start:copy-delete]
        # Copy object
        await client.s3.copy(
            BUCKET, "examples/record-copy.json", BUCKET, "examples/record.json"
        )
        print("Copied    → examples/record.json → examples/record-copy.json")

        # Delete objects
        await client.s3.delete(BUCKET, "examples/record.json")
        await client.s3.delete(BUCKET, "examples/record-copy.json")
        print("Deleted   → examples/record.json, examples/record-copy.json")
        # --8<-- [end:copy-delete]

        # --8<-- [start:list-buckets]
        # List all buckets
        buckets = await client.s3.list_buckets()
        print(f"Buckets   → {buckets}")
        # --8<-- [end:list-buckets]


if __name__ == "__main__":
    asyncio.run(main())
