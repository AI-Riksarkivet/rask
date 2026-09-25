"""Wait for an S3-compatible store to answer, then create each named bucket it does not hold yet.

WHY THIS EXISTS RATHER THAN `mc`. Both the Dagger lanes and the chart's bucket Job bootstrapped the
object store by pulling `minio/mc`. Measured 2026-09-24 with the bearer DECODED, that image refuses
ANONYMOUS pulls on every registry the estate can reach: Docker Hub issues a token whose `access` is
`[]`, quay.io one with `actions: []`, and both answer 401 on the manifest. So a lane that pulls fresh
cannot start its store at all, and an install that pulls fresh gets a bucket Job stuck in
ImagePullBackOff ([[XC-075]]).

The estate already owns this: `packages/storage`'s `s3_client` is the S3 seam the fleet uses, in a
first-party image no registry can withdraw. Two S3 calls replace a third-party CLI, and the chart's
Job runs this file out of the rask image exactly as the backup CronJob runs `control_root_backup.py`.

THE WAIT IS THE OTHER HALF OF WHAT `mc` WAS DOING. The `until mc alias set …; do sleep 2; done` loop
was not decoration: a Dagger service binding resolves as soon as the container starts, which is before
the store is listening, and in-cluster the Job is scheduled alongside the store it talks to. Without a
retry here the create races the boot and fails on the first run of a cold lane.

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


def _client() -> S3Client:
    # Imported inside the function so `--help` and the argument errors above do not pay for boto3.
    from storage import s3_client

    return s3_client()


def _await_store(client: S3Client) -> None:
    """Block until the store answers, or exit non-zero naming how long it waited."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            client.list_buckets()
            return
        except Exception as exc:
            if attempt == _ATTEMPTS:
                raise SystemExit(f"!! the object store never answered after {_ATTEMPTS * _DELAY_SECONDS:.0f}s: {exc}") from exc
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
        # IDEMPOTENT, like `mb --ignore-existing`: a lane re-run against a warm volume, or a helm
        # upgrade re-running this Job, must not fail on a bucket it created last time.
        if _exists(client, bucket):
            print(f"ensure_bucket: {bucket} already exists")
            continue
        # A failed create RAISES, so the Job exits non-zero rather than going green with a bucket
        # absent — live-proof 2026-07-28 defect 3, where that first surfaced as an HTTP 500 in the
        # storage browser three services away.
        client.create_bucket(Bucket=bucket)
        print(f"ensure_bucket: created {bucket}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("buckets", nargs="+", help="buckets to create if absent")
    args = parser.parse_args(argv[1:])

    client = _client()
    _await_store(client)
    _create(client, args.buckets)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
