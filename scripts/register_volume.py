"""Register an uploaded volume by POSTing to core-api.

Usage:
    uv run python scripts/register_volume.py VOL_A [VOL_B ...] \
        --base-url http://localhost:8888

Images must already be in the input bucket under `<volume_id>/`. This only
indexes them into the batches table; it does not upload.
"""

import argparse
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Register uploaded S3 volumes into rask.")
    parser.add_argument("volume_ids", nargs="+", help="Volume ids = input-bucket prefixes (e.g. VOL_A).")
    parser.add_argument("--base-url", default="http://localhost:8888", help="Gateway/core-api base URL.")
    parser.add_argument("--api-prefix", default="/api/v1")
    args = parser.parse_args()

    rc = 0
    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        for vol in args.volume_ids:
            resp = client.post(f"{args.api_prefix}/batches/{vol}/register")
            if resp.status_code == 201:
                body = resp.json()
                print(f"registered {vol}: page_count={body['page_count']} chunk_id={body['chunk_id']}")
            else:
                print(f"FAILED {vol}: {resp.status_code} {resp.text}", file=sys.stderr)
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
