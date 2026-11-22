"""The store's failures in the library's own words, so no caller reads a botocore exception.

:func:`store_call` is the one place the library reads what the S3 client raised: it maps what the store answered (a
``ClientError``) and a call that got no answer (a transport failure) to the :class:`StorageError` family, always chained
(``raise ... from exc``), so the client's own failure stays the cause a reader of an outage can still consult. The family
derives from ``OSError``, the standard type of an operational I/O failure, which pyarrow and Lance raise for the same
failures, so an ``except OSError`` already written around a store call catches it unchanged. Any other failure of the
client's (a parameter it refuses, a credential it cannot find) is a defect of the call, not an answer of the store's,
and passes through as itself: mapped, an ``except OSError`` would answer it as a refusal to retry around.

The codes read below are standard S3 REST error codes, which AWS and RustFS both answer: ``NoSuchBucket``, ``NoSuchKey``,
and on a ``HEAD``, which has no body to carry a code, the bare status ``404``/``NotFound``. ``PreconditionFailed`` (412) is
a conditional write whose condition did not hold: the store's verdict on the key. ``ConditionalRequestConflict`` (409) is
a conditional write that met another write of the same key in flight, which the store has not settled: no verdict yet,
so it is a class of its own, and any other 409 is read as it (the status says the conflict, and a store may name it by a
code of its own, as AWS also answers ``OperationAborted``).

Which failure is an outage lives here once, in :func:`no_answer`, and :mod:`lakehouse.problems.outage` reads it: no
response, a 5xx other than 501, or the store's rate limit (429). A 501 is the store saying it lacks what was asked,
which no retry clears.

Usage: wrap the call, not the handler, so only the store's failures are mapped::

    with store_call(f"reading {bucket}/{key}"):
        resp = client.head_object(Bucket=bucket, Key=key)
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any


__all__ = [
    "BucketNotFoundError",
    "MissingStoreCredentialError",
    "ObjectNotFoundError",
    "PreconditionFailedError",
    "StorageError",
    "StoreRefusedError",
    "StoreUnansweredError",
    "WriteConflictError",
    "no_answer",
    "store_call",
]

_BUCKET_MISSING = frozenset({"NoSuchBucket"})
_KEY_MISSING = frozenset({"NoSuchKey", "404", "NotFound"})
_CONDITION_FAILED = frozenset({"PreconditionFailed"})
_WRITE_IN_FLIGHT = frozenset({"ConditionalRequestConflict"})


class StorageError(OSError):
    """Raised when the object store failed a call; ``response`` is what it answered, empty when it answered nothing."""

    def __init__(self, message: str, *, response: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response: Mapping[str, Any] = response or {}


class ObjectNotFoundError(StorageError, FileNotFoundError):
    """Raised when the store holds no object at the key a call named (404, ``NoSuchKey``, ``NotFound``)."""


class BucketNotFoundError(StorageError):
    """Raised when the bucket a call named does not exist (``NoSuchBucket``): an empty bucket lists, an absent one does not."""


class PreconditionFailedError(StorageError):
    """Raised when the store refused a conditional write because its condition did not hold (412)."""


class WriteConflictError(StorageError):
    """Raised when a write met another write of the same key in flight (409): the store has settled neither, so this is
    no verdict on the key, and a read after it sees what the store settled."""


class StoreRefusedError(StorageError):
    """Raised when the store answered a call with a refusal a retry meets again."""


class StoreUnansweredError(StorageError):
    """Raised when the store did not answer a call: no response, a server error other than 501, or its rate limit."""


class MissingStoreCredentialError(Exception):
    """Raised when a store client is asked for without a whole key pair to sign with, so none is built.

    botocore and pyarrow fill a missing credential from their default chain (environment variables, shared files,
    profiles, a credential process, container and instance metadata), and in a pod that chain signs as a credential
    nobody scoped. A deployment defect, not a store failure, so it is no ``OSError`` an ``except OSError`` would read as
    a refusal. The message names the missing parts, never a value.
    """


def no_answer(exc: BaseException) -> bool:
    """Whether ``exc`` reports the store not answering: no response, a 5xx other than 501, or a 429."""
    from botocore.exceptions import ClientError, HTTPClientError
    from botocore.exceptions import ConnectionError as NoConnectionError

    if isinstance(exc, StoreUnansweredError | NoConnectionError | HTTPClientError):
        return True
    if not isinstance(exc, ClientError):
        return False
    return _status_unanswered(_status(exc.response))


def _status(response: Mapping[str, Any]) -> int:
    return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


def _status_unanswered(status: int) -> bool:
    return status == HTTPStatus.TOO_MANY_REQUESTS or (status >= HTTPStatus.INTERNAL_SERVER_ERROR and status != HTTPStatus.NOT_IMPLEMENTED)


def _answered(response: Mapping[str, Any], doing: str) -> StorageError:
    """The family's error for the store's ``response`` to the call ``doing``."""
    code = str(response.get("Error", {}).get("Code", ""))
    status = _status(response)
    if code in _BUCKET_MISSING:
        return BucketNotFoundError(f"{doing}: the bucket does not exist", response=response)
    if code in _KEY_MISSING or status == HTTPStatus.NOT_FOUND:
        return ObjectNotFoundError(f"{doing}: no such object", response=response)
    if code in _CONDITION_FAILED or status == HTTPStatus.PRECONDITION_FAILED:
        return PreconditionFailedError(f"{doing}: the store's condition did not hold", response=response)
    if code in _WRITE_IN_FLIGHT or status == HTTPStatus.CONFLICT:
        return WriteConflictError(f"{doing}: another write of the key was in flight", response=response)
    if _status_unanswered(status):
        return StoreUnansweredError(f"{doing}: the store answered {status} {code}", response=response)
    return StoreRefusedError(f"{doing}: the store refused it, {status} {code}", response=response)


@contextmanager
def store_call(doing: str) -> Generator[None]:
    """Map the store's answer to a call inside the block, and a call that got none, to the :class:`StorageError` family,
    chained.

    ``doing`` says what the call was for (``"reading <bucket>/<key>"``), for the message. Anything else passes through
    untouched: a failure that is not the S3 client's, and one of the client's that is neither (a defect of the call).

    Raises:
        StorageError: The store answered the call with a failure, or did not answer it; the client's own exception is
            the ``__cause__``.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        yield
    except ClientError as exc:
        raise _answered(exc.response, doing) from exc
    except BotoCoreError as exc:
        if not no_answer(exc):
            raise
        raise StoreUnansweredError(f"{doing}: the store did not answer: {exc}") from exc
