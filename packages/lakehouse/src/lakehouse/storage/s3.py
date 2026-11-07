"""Key listing and timestamped-prefix retention over an S3 client.

The ``list_objects_v2`` pagination lives once, in :func:`_paginate_keys`, and every S3 call is wrapped in
:func:`~lakehouse.storage.errors.s3_errors`, so a missing bucket reaches a caller as storage's own
:class:`~lakehouse.storage.errors.BucketNotFoundError` rather than a raw botocore ``ClientError`` it cannot name.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from lakehouse.storage.errors import s3_errors


if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def _paginate_keys(client: S3Client, bucket: str, prefix: str = "") -> Iterator[str]:
    """Every key under ``bucket``/``prefix``, page by page, in listing order.

    Bucket-scoped (no ``key``), so a not-found inside the listing is reported against the bucket —
    which is the only thing it can be about: an empty bucket lists successfully.
    """
    with s3_errors(bucket=bucket):
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def iter_keys(client: S3Client, bucket: str, prefix: str = "", suffix: str = "") -> Iterator[str]:
    """Yield keys under `bucket`/`prefix`, optionally filtered by case-insensitive `suffix`.

    Sync — wrap in `anyio.to_thread.run_sync` if called from an event loop.
    """
    suffix_lc = suffix.lower()
    for key in _paginate_keys(client, bucket, prefix):
        if not suffix_lc or key.lower().endswith(suffix_lc):
            yield key


def prune_timestamped_prefixes(client: S3Client, *, bucket: str, base: str, keep: int) -> tuple[list[str], list[str]]:
    """Keep the newest `keep` timestamped prefixes directly under `base`; delete the rest.

    Returns `(kept, pruned)`, newest first. `keep=0` means UNBOUNDED and deletes nothing — a tool
    that quietly started deleting backups the first time it was upgraded would be a worse failure
    than the growth it fixes.

    NEWEST-FIRST IS A REVERSE LEXICAL SORT, which is a property of the stamp format rather than of
    this function: `%Y%m%dT%H%M%SZ` and `%Y-%m-%dT%H-%M-%SZ` both order lexically as they order in
    time. Callers that stamp differently must not use this.

    IT LIVES HERE BECAUSE TWO BACKUP LANES NEED THE SAME ANSWER. The control-root tool and the
    Postgres dump both retain N newest under a prefix, and the second was a shell pipeline
    (`ls | sort -r | tail -n +N`) whose agreement with the first was a comment rather than a shared
    implementation — "an operator reading one and reasoning about the other must not meet two
    different answers to 'which is the newest'".
    """
    marker = base if base.endswith("/") or not base else base + "/"
    stamps = sorted(
        {stamp for key in iter_keys(client, bucket, marker) if (stamp := key[len(marker) :].split("/", 1)[0])},
        reverse=True,
    )
    if keep <= 0:
        return stamps, []
    kept, pruned = stamps[:keep], stamps[keep:]
    for stamp in pruned:
        for key in list(iter_keys(client, bucket, f"{marker}{stamp}/")):
            with s3_errors(bucket=bucket, key=key):
                client.delete_object(Bucket=bucket, Key=key)
    return kept, pruned
