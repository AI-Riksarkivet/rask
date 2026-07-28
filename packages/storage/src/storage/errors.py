"""The backend-agnostic S3 error taxonomy — so no caller ever imports botocore.

``packages/storage`` is the ONE S3 wrapper in this estate (never raw boto3), and until
this module the wrapper leaked its own boundary: a caller that wanted to tell "that
bucket does not exist" from "that key does not exist" from "the store is down" had to
reach into ``botocore.exceptions.ClientError`` and read ``response["Error"]["Code"]``.
Nobody did, so every caller either caught nothing (→ an unhandled botocore exception
became an HTTP 500 with a traceback: the lakehouse storage browser's "Storage service
unreachable (HTTP 500)" on a missing bucket, live-proof 2026-07-28) or caught
``Exception`` and flattened three very different situations into one message.

The codes matched below are **standard S3 REST error codes**, not a vendor dialect —
AWS, MinIO and RustFS all return `NoSuchBucket` / `NoSuchKey`, and a `HEAD` (which has
no body to carry a code) surfaces as the bare HTTP status `404`/`NotFound` on every one
of them. That keeps the storage-agnostic contract intact: swapping backends stays an
env change.

Usage — wrap the call, not the whole handler, so only storage failures are translated::

    with s3_errors(bucket=bucket, key=key):
        resp = client.head_object(Bucket=bucket, Key=key)

Anything that is not a recognised S3 error passes through untouched: a genuinely
unreachable endpoint or a credential failure is NOT a "not found", and pretending
otherwise would hide an outage behind a 404.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


__all__ = [
    "BucketNotFoundError",
    "ObjectNotFoundError",
    "StorageError",
    "s3_errors",
]

#: Error codes an S3 backend uses for "no such bucket". `404`/`NotFound` never appear
#: here: a bucket-scoped HEAD is not something this estate issues, and a bare 404 from a
#: key operation must not be mistaken for a missing bucket.
_BUCKET_MISSING = frozenset({"NoSuchBucket"})

#: Error codes for "no such key". `NoSuchKey` is the GET/LIST form; `404` and `NotFound`
#: are what a HEAD returns, where there is no response body to carry a code.
_KEY_MISSING = frozenset({"NoSuchKey", "404", "NotFound"})


class StorageError(Exception):
    """Base for the storage boundary's own, backend-neutral failures."""


class BucketNotFoundError(StorageError):
    """The bucket itself does not exist on the configured S3 endpoint.

    Distinct from :class:`ObjectNotFoundError` on purpose: an empty bucket and an
    ABSENT bucket look identical in a listing, and conflating them is how a
    never-provisioned bucket stayed invisible for a whole debugging session.
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        super().__init__(f"bucket does not exist: {bucket}")


class ObjectNotFoundError(StorageError):
    """The bucket exists but holds no such key."""

    def __init__(self, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key
        super().__init__(f"object does not exist: {bucket}/{key}")


def _error_code(exc: BaseException) -> str | None:
    """The S3 error code carried by a botocore ``ClientError``, if this is one.

    Duck-typed rather than ``isinstance(exc, ClientError)`` so this module imports no
    botocore at all (boto3 is an optional-at-import dependency of several callers, and
    the taxonomy must be usable by a stubbed client in tests).
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


@contextmanager
def s3_errors(*, bucket: str, key: str | None = None) -> Iterator[None]:
    """Translate S3 not-found errors raised inside the block into :class:`StorageError`.

    `key` is optional: a listing has no key, so a `404` inside a listing block is
    reported against the bucket. Every other exception — connection errors, auth
    failures, throttling — propagates unchanged, because those are outages and deserve
    a 5xx, not a polite 404.
    """
    try:
        yield
    except Exception as exc:
        code = _error_code(exc)
        if code in _BUCKET_MISSING:
            raise BucketNotFoundError(bucket) from exc
        if code in _KEY_MISSING:
            if key is None:
                raise BucketNotFoundError(bucket) from exc
            raise ObjectNotFoundError(bucket, key) from exc
        raise
