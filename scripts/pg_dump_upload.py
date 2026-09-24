"""Upload a finished Postgres dump directory to S3 under a timestamped prefix, then prune old ones.

WHY THIS EXISTS RATHER THAN `mc`. The chart's `backups.pgDump` CronJob ran `minio/mc` —
`mc cp --recursive`, `mc ls | sort -r | tail -n +N`, `mc rm --recursive`. Measured 2026-09-24 with
the bearer DECODED, that image refuses ANONYMOUS pulls on every registry this estate can reach:
Docker Hub issues a token whose `access` is `[]`, quay.io one with `actions: []`, both 401 on the
manifest ([[XC-075]]). The dump it uploads is the lineage graph (AGE) and the authorization store
(OpenFGA), so on a node that pulls fresh the estate's two most irreplaceable databases back up
nowhere — and a CronJob that cannot start says so once, in an event nobody reads.

Every verb it used is plain S3: `cp` is `put_object`, `ls` + `rm` are the retention walk that
`storage.prune_timestamped_prefixes` now owns for both backup lanes.

    RASK_S3_ENDPOINT_URL=… AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \\
        uv run python scripts/pg_dump_upload.py /dump s3://bucket/_backups/pg --keep 7
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import UTC, datetime


def _stamp() -> str:
    """The run's prefix. `%Y-%m-%dT%H-%M-%SZ` sorts lexically as it sorts in time, which is what
    `prune_timestamped_prefixes` relies on — and is the format the shell lane already wrote, so a
    store holding backups from both lanes stays in one order."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Upload a dump directory to S3 and prune old runs.")
    parser.add_argument("source", type=pathlib.Path, help="the local directory to upload")
    parser.add_argument("dest", help="s3://bucket/prefix to upload under")
    parser.add_argument("--keep", type=int, default=0, help="retain this many newest runs (0 = unbounded)")
    args = parser.parse_args(argv[1:])

    if not args.source.is_dir():
        print(f"!! {args.source} is not a directory — nothing was dumped", file=sys.stderr)
        return 1
    files = sorted(path for path in args.source.rglob("*") if path.is_file())
    # AN EMPTY DUMP IS A FAILURE, NOT A NO-OP. `pg_dump` writing nothing and this exiting 0 would
    # retire a good backup on the next prune and leave the estate believing it has one.
    if not files:
        print(f"!! {args.source} holds no files — refusing to record an empty backup", file=sys.stderr)
        return 1

    from storage import prune_timestamped_prefixes, s3_client, split_s3_uri

    bucket, base = split_s3_uri(args.dest)
    stamp = _stamp()
    client = s3_client()
    for path in files:
        key = f"{base.rstrip('/')}/{stamp}/{path.relative_to(args.source).as_posix()}"
        with path.open("rb") as handle:
            client.put_object(Bucket=bucket, Key=key, Body=handle)
    print(f"pg_dump_upload: uploaded {len(files)} file(s) to s3://{bucket}/{base.rstrip('/')}/{stamp}/")

    kept, pruned = prune_timestamped_prefixes(client, bucket=bucket, base=base.rstrip("/"), keep=args.keep)
    print(f"pg_dump_upload: kept {len(kept)} run(s), pruned {len(pruned)}{': ' + ' '.join(pruned) if pruned else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
