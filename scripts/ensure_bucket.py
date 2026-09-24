"""Wait for an S3-compatible store to answer, then create a bucket if it is not there.

WHY THIS EXISTS RATHER THAN `mc`. The Dagger lanes bootstrapped their object store by pulling
`minio/mc` and running `mc alias set … && mc mb --ignore-existing`. Measured 2026-09-24, that image
refuses ANONYMOUS pulls on every registry the estate could get it from: Docker Hub issues a token
whose `access` is `[]`, quay.io one with `actions: []` and a `401` on the manifest. A lane that pulls
fresh — which every Dagger lane does — cannot start its store at all, and `e2e-auth` failed on the
resolve rather than on anything it tests ([[XC-075]]).

The estate already owns this: `packages/storage`'s `s3_client` is the S3 seam the fleet uses, in a
first-party image no registry can withdraw. Two S3 calls replace a third-party CLI.

THE WAIT IS THE OTHER HALF OF WHAT `mc` WAS DOING. The `until mc alias set …; do sleep 2; done` loop
was not decoration: a Dagger service binding resolves as soon as the container starts, which is before
the store is listening. Without a retry here the create races the boot and fails on the first run of a
cold lane.

    RASK_S3_ENDPOINT_URL=http://store:9000 AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \
        uv run python scripts/ensure_bucket.py lance-catalog
"""

from __future__ import annotations

import sys
import time


#: Bounded, and the bound is stated: a store that has not answered in two minutes is not slow, it is
#: not coming, and a lane that hangs instead of failing costs a whole CI run to diagnose.
_ATTEMPTS = 60
_DELAY_SECONDS = 2.0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: ensure_bucket.py <bucket>", file=sys.stderr)
        return 2
    bucket = argv[1]

    from storage import s3_client

    client = s3_client()
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            client.list_buckets()
            break
        except Exception as exc:
            last = exc
            if attempt == _ATTEMPTS:
                print(f"!! the object store never answered after {_ATTEMPTS * _DELAY_SECONDS:.0f}s: {exc}", file=sys.stderr)
                return 1
            time.sleep(_DELAY_SECONDS)
    else:  # pragma: no cover - the loop always breaks or returns
        print(f"!! unreachable: {last}", file=sys.stderr)
        return 1

    # IDEMPOTENT, like `mb --ignore-existing`: a lane re-run against a warm volume must not fail on a
    # bucket it created last time.
    try:
        client.head_bucket(Bucket=bucket)
        print(f"ensure_bucket: {bucket} already exists")
    except Exception:
        client.create_bucket(Bucket=bucket)
        print(f"ensure_bucket: created {bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
