"""Wait for an S3-compatible store to answer, then create each named bucket it does not hold yet.

The Dagger lanes bootstrap their throwaway store with this file, through `packages/storage`'s `s3_client`, the S3 seam
the fleet uses, so no lane depends on a third-party CLI image a registry can withdraw ([[XC-075]]). In a cluster the
scoped-users hook makes the platform buckets with the same two calls (`scripts/rustfs_admin.py`).

THE WAIT IS HALF THE JOB. A Dagger service binding resolves as soon as the container starts, which is before the
store is listening. Without a bounded retry here the create races the boot and fails on the first run of a cold lane.
Only an unreachable or not-yet-ready store is waited out; a refusal, such as a wrong key, fails at once.

The credentials come from the standard AWS env chain (the lane's ephemeral store).

    RASK_S3_ENDPOINT_URL=http://store:9000 AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \
        uv run python scripts/ensure_bucket.py lance-catalog rask-observability
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from storage import S3Client


#: Bounded, and the bound is stated: a store that has not answered in two minutes is not slow, it is
#: not coming, and a lane that hangs instead of failing costs a whole CI run to diagnose.
_ATTEMPTS = 60
_DELAY_SECONDS = 2.0


def _await_store(client: S3Client) -> None:
    """Block until the store answers, or exit non-zero naming how long it waited or what it refused."""
    from botocore.exceptions import ClientError, HTTPClientError
    from botocore.exceptions import ConnectionError as StoreUnreachableError

    for attempt in range(1, _ATTEMPTS + 1):
        try:
            client.list_buckets()
            return
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) < 500:
                raise SystemExit(f"!! the object store refused the credentials: {exc}") from exc
            failure: Exception = exc
        except (StoreUnreachableError, HTTPClientError) as exc:
            failure = exc
        if attempt == _ATTEMPTS:
            raise SystemExit(f"!! the object store never answered after {_ATTEMPTS * _DELAY_SECONDS:.0f}s: {failure}") from failure
        time.sleep(_DELAY_SECONDS)


def _exists(client: S3Client, bucket: str) -> bool:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        return False
    return True


def _create(client: S3Client, buckets: list[str]) -> None:
    # DEDUPED IN ORDER: a caller's list may name one bucket twice, and a second pass is only log noise.
    for bucket in dict.fromkeys(buckets):
        # IDEMPOTENT: a lane re-run against a warm volume must not fail on a bucket it created last time.
        if _exists(client, bucket):
            print(f"ensure_bucket: {bucket} already exists")
            continue
        # A failed create RAISES, so the lane exits non-zero rather than going green with a bucket
        # absent — live-proof 2026-07-28 defect 3, where that first surfaced as an HTTP 500 in the
        # storage browser three services away.
        client.create_bucket(Bucket=bucket)
        print(f"ensure_bucket: created {bucket}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("buckets", nargs="+", help="buckets to create if absent")
    args = parser.parse_args(argv[1:])

    # Imported here so `--help` and the argument errors above do not pay for boto3.
    from storage import s3_client

    client = s3_client()
    _await_store(client)
    _create(client, args.buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
