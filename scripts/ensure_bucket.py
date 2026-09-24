"""Wait for an S3-compatible store to answer, then CREATE buckets and/or VERIFY ones it should already hold.

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
        uv run python scripts/ensure_bucket.py lance-catalog
    … ensure_bucket.py lance-catalog obs --verify tenant-a tenant-b --verify-timeout 60
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

#: The VERIFY poll's step. Provisioning is asynchronous, so the timeout is expressed in seconds by the
#: caller and divided by this rather than the caller counting ticks.
_VERIFY_STEP_SECONDS = 5.0


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
    # DEDUPED IN ORDER: the chart's create list is assembled from several values blocks that may name
    # the same bucket (the shared `minio.bucket` also appears in the external-S3 static set), and a
    # second pass is only log noise — `head_bucket` already makes each create idempotent.
    for bucket in dict.fromkeys(buckets):
        # IDEMPOTENT, like `mb --ignore-existing`: a lane re-run against a warm volume, or a helm
        # upgrade re-running this Job, must not fail on a bucket it created last time.
        if _exists(client, bucket):
            print(f"ensure_bucket: {bucket} already exists")
            continue
        client.create_bucket(Bucket=bucket)
        print(f"ensure_bucket: created {bucket}")


def _verify(client: S3Client, buckets: list[str], timeout_seconds: float, hint: str) -> int:
    """Poll for buckets SOMETHING ELSE owns, then fail loudly naming every one still missing.

    DELIBERATELY NOT A CREATE, and the asymmetry is the whole point. These are the operator's
    (`minio.buckets` -> the Tenant's `spec.buckets`): live-proof 2026-07-28, an operator refused to
    reconcile its Tenant (`StatefulSetUpdateValidationFailed`) so `spec.buckets` was never
    provisioned, and NOTHING said so — the store answered, this Job went green because it only ever
    created its own buckets, and the single symptom surfaced three layers up as an HTTP 500 in the
    storage browser. Creating them here would paper over a Tenant that is still broken for every
    OTHER thing it owns, and the next failure would be further from its cause than this one was.
    """
    deadline = max(1, int(timeout_seconds / _VERIFY_STEP_SECONDS))
    missing: list[str] = []
    for tick in range(deadline):
        missing = [bucket for bucket in buckets if not _exists(client, bucket)]
        if not missing:
            print(f"ensure_bucket: all {len(buckets)} operator-owned buckets present")
            return 0
        if tick + 1 < deadline:
            print(f"waiting for the store to provision: {' '.join(missing)}")
            time.sleep(_VERIFY_STEP_SECONDS)
    print(
        f"!! the store never provisioned these buckets it owns: {' '.join(missing)}\n"
        "!! they are the operator's (minio.buckets -> the Tenant's spec.buckets), so this Job will not "
        "create them — a Tenant that cannot reconcile one bucket cannot reconcile the rest either.",
        file=sys.stderr,
    )
    # THE DIAGNOSIS BELONGS TO THE CALLER. This script knows S3; only the deployment knows which
    # StatefulSet to describe and which value is the usual cause. Passing it in keeps the chart's
    # cluster-specific guidance on the failure it explains, without teaching an S3 helper kubectl.
    if hint:
        print(hint, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("buckets", nargs="*", help="buckets to CREATE if absent")
    parser.add_argument("--verify", nargs="*", default=[], help="buckets something else owns; poll, never create")
    parser.add_argument("--verify-timeout", type=float, default=60.0, help="seconds to wait for the verified set")
    parser.add_argument("--missing-hint", default="", help="caller-supplied diagnosis printed when --verify fails")
    args = parser.parse_args(argv[1:])
    if not args.buckets and not args.verify:
        parser.error("nothing to do: name at least one bucket to create or --verify")

    client = _client()
    _await_store(client)
    _create(client, args.buckets)
    return _verify(client, args.verify, args.verify_timeout, args.missing_hint) if args.verify else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
