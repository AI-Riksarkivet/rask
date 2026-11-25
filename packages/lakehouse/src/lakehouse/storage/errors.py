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
so it is a class of its own, and any other 409 about a key is read as it (the status says the conflict, and a store may
name it by a code of its own, as AWS also answers ``OperationAborted``). A 409 about a bucket is no write in flight: a
create of a bucket that is there (``BucketAlreadyOwnedByYou``, and ``BucketAlreadyExists``, which AWS answers for a name
another account holds) is a class of its own, and a delete of a bucket still holding objects (``BucketNotEmpty``) is the
store's refusal.

Which failure is an outage lives here once, in :func:`no_answer`, and :mod:`lakehouse.problems.outage` reads it: no
response, a 5xx other than 501, the store's rate limit (429), or a code by which S3 says it did not get to what was
asked, under any status (:func:`unanswered_code`). A 501 is the store saying it lacks what was asked, which no retry
clears.

Usage: wrap the call, not the handler, so only the store's failures are mapped::

    with store_call(f"reading {bucket}/{key}"):
        resp = client.head_object(Bucket=bucket, Key=key)
"""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from typing import Any


__all__ = [
    "BucketExistsError",
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
    "unanswered_code",
]

_BUCKET_MISSING = frozenset({"NoSuchBucket"})
_BUCKET_EXISTS = frozenset({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"})
_BUCKET_NOT_EMPTY = frozenset({"BucketNotEmpty"})
_KEY_MISSING = frozenset({"NoSuchKey", "404", "NotFound"})
_CONDITION_FAILED = frozenset({"PreconditionFailed"})
_WRITE_IN_FLIGHT = frozenset({"ConditionalRequestConflict"})
#: The codes by which S3 says it did not get to what was asked: its own failure, its rate limit, its being away, and its
#: giving up on a request it had not finished receiving, which it answers as a 400. The S3 client retries each of them
#: itself, by code or by status (``botocore.retries.standard``, read on botocore 1.43.58), so one that reaches a caller
#: outlasted those retries.
_UNANSWERED = frozenset({"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"})


class StorageError(OSError):
    """Raised when the object store failed a call; ``response`` is what it answered, empty when it answered nothing."""

    def __init__(self, message: str, *, response: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response: Mapping[str, Any] = response or {}


class ObjectNotFoundError(StorageError, FileNotFoundError):
    """Raised when the store holds no object at the key a call named (404, ``NoSuchKey``, ``NotFound``)."""


class BucketNotFoundError(StorageError):
    """Raised when the bucket a call named does not exist (``NoSuchBucket``): an empty bucket lists, an absent one does not."""


class BucketExistsError(StorageError):
    """Raised when a create met a bucket of that name (409). The store does not say here whose it is where names are
    shared between accounts, so a caller that must own the bucket learns it by writing to it."""


class PreconditionFailedError(StorageError):
    """Raised when the store refused a conditional write because its condition did not hold (412)."""


class WriteConflictError(StorageError):
    """Raised when a write met another write of the same key in flight (409): the store has settled neither, so this is
    no verdict on the key, and a read after it sees what the store settled."""


class StoreRefusedError(StorageError):
    """Raised when the store answered a call with a refusal a retry meets again."""


class StoreUnansweredError(StorageError):
    """Raised when the store did not answer a call: no response, a server error other than 501, its rate limit, or a
    code that says it did not get to the request (:func:`unanswered_code`)."""


class MissingStoreCredentialError(Exception):
    """Raised when a store client is asked for without a whole key pair to sign with, so none is built.

    botocore and pyarrow fill a missing credential from their default chain (environment variables, shared files,
    profiles, a credential process, container and instance metadata), and in a pod that chain signs as a credential
    nobody scoped. A deployment defect, not a store failure, so it is no ``OSError`` an ``except OSError`` would read as
    a refusal. The message names the missing parts, never a value.
    """


def no_answer(exc: BaseException) -> bool:
    """Whether ``exc`` reports the store not answering: no response, a 5xx other than 501, a 429, or a code that says the
    store did not get to the request (:func:`unanswered_code`)."""
    from botocore.exceptions import ClientError, HTTPClientError
    from botocore.exceptions import ConnectionError as NoConnectionError

    if isinstance(exc, StoreUnansweredError | NoConnectionError | HTTPClientError):
        return True
    if not isinstance(exc, ClientError):
        return False
    return _unanswered(exc.response)


def unanswered_code(code: str) -> bool:
    """Whether ``code``, an S3 error code, says the store did not get to what was asked of it.

    For an answer that carries a code and no status of its own, as each key's error in a batch delete's answer does; a
    whole request's answer is read by :func:`no_answer`, which asks this too.
    """
    return code in _UNANSWERED


def _status(response: Mapping[str, Any]) -> int:
    return int(response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


def _code(response: Mapping[str, Any]) -> str:
    return str(response.get("Error", {}).get("Code", ""))


def _unanswered(response: Mapping[str, Any]) -> bool:
    """Whether the store's ``response`` says it did not get to the request: by its status, or by its code under any status."""
    status = _status(response)
    by_status = status == HTTPStatus.TOO_MANY_REQUESTS or (status >= HTTPStatus.INTERNAL_SERVER_ERROR and status != HTTPStatus.NOT_IMPLEMENTED)
    return by_status or unanswered_code(_code(response))


def _answered(response: Mapping[str, Any], doing: str) -> StorageError:
    """The family's error for the store's ``response`` to the call ``doing``."""
    code, status = _code(response), _status(response)
    if code in _BUCKET_MISSING:
        return BucketNotFoundError(f"{doing}: the bucket does not exist", response=response)
    if code in _BUCKET_EXISTS:
        return BucketExistsError(f"{doing}: a bucket of that name is there", response=response)
    if code in _KEY_MISSING or status == HTTPStatus.NOT_FOUND:
        return ObjectNotFoundError(f"{doing}: no such object", response=response)
    if code in _CONDITION_FAILED or status == HTTPStatus.PRECONDITION_FAILED:
        return PreconditionFailedError(f"{doing}: the store's condition did not hold", response=response)
    if code in _WRITE_IN_FLIGHT or (status == HTTPStatus.CONFLICT and code not in _BUCKET_NOT_EMPTY):
        return WriteConflictError(f"{doing}: another write of the key was in flight", response=response)
    if _unanswered(response):
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
