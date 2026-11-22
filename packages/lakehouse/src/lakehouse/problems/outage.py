"""ONE answer to "did this object-store call, Lance's or pyarrow's, fail because the store did not answer?".

The opposite question from absence: absence PROVES a target is not there, while an outage proves nothing
about the target, only that the store could not be asked. The
two must never be confused in either direction: an outage read as absence is how a black-holed store
became "table not found" (404), and an outage read as a broken catalog is how it became a 500 that
sends whoever is on call to the wrong system. 503 is the status that says what happened and asks the
caller to retry.

TEXT, NOT TYPES, because object_store flattens every failure into a bare
`ValueError`/`OSError` carrying `LanceError(IO)` and the reason in the message, and no type tells an outage from the
caller's bad bytes: measured on pylance 13.0.0, a footer read raises `OSError` both for a file that is not a Lance file
and for a store that does not answer, and an open raises `ValueError` both for a missing dataset and for that store
(`lance.commit.CommitConflictError` types a commit conflict only, `lancekit.commit_verdict`). MEASURED on pylance
12.0.0, opening a dataset through each store fault (`lance.dataset`, bounded client timeouts):

    connect never completes (packets dropped)   "... Error performing GET <url> in 2.1s ... - HTTP error: error sending request"
    accepted, never answered                    "... Error performing GET <url> in 3.0s - HTTP error: error sending request"
    body cut off by the request bound           "... Generic S3 error: HTTP error: request or response body error, ..." (a full read
                                                through a store capped at 8 MB/s under a 5 s request bound)
    503 on every request                        "... - Server returned non-2xx status code: 503 Service Unavailable: ..."
    500 on every request                        "... - Server returned non-2xx status code: 500 Internal Server Error: ..."
    403 on every request                        "... - Server returned non-2xx status code: 403 Forbidden: ..."
    404 NoSuchBucket                            "... - Server returned non-2xx status code: 404 Not Found: <Code>NoSuchBucket</Code> ..."

The NATIVE namespace backend says the same in another dialect: it raises a typed `InternalError`
(code 18, which maps to 500) whose message is the Rust error's Debug form. Measured on pylance 12.0.0,
`DirectoryNamespace.describe_table` on a namespace built while the store answered, after it stopped:

    accepted, never answered     "IO { source: Generic { store: "S3", source: RetryError(... inner: Http(HttpError { kind: Timeout, ..."
    connection dropped           "IO { source: Generic { store: "S3", source: RetryError(... inner: Http(HttpError { kind: Request, ..."
    503 on every request         "IO { source: Generic { store: "S3", source: RetryError(... inner: Status { status: 503, ..."
    403 on every request         "IO { source: PermissionDenied { path: ..., ... inner: Status { status: 403, ..."

Measured again on pylance 14.0.3 (2026-11-22), a proxy in front of moto answering every request with the status after a
`dir` connection made a row, so the connection had opened its index: a 429 `TooManyRequests` raises the spec's
``InternalError`` "Internal error: Failed to check dataset staleness: IO { source: Generic { store: "S3", source:
RetryError(RetryErrorImpl { method: HEAD, ..., inner: Status { status: 429, body: Some("") } }) }, ...", and on a fresh
connection the flattened "... - Server returned non-2xx status code: 429 Too Many Requests: ...".

The catalog's own reads and writes of a table's objects go through pyarrow's S3 filesystem (aws-sdk-cpp), a third
dialect: ``When <doing what> ... in bucket '<b>': AWS Error <CODE> during <Operation> operation: <message>``. Measured on
pyarrow 25.0.0 against a store answering each fault to a HEAD, a multipart PUT and a listing ([[LH-545]]):

    500 on every request         "AWS Error INTERNAL_FAILURE during ...", "AWS Error UNKNOWN (HTTP status 500) during HeadObject ..."
    503 ServiceUnavailable       "AWS Error SERVICE_UNAVAILABLE during ...", "AWS Error UNKNOWN (HTTP status 503) during HeadObject ..."
    503 SlowDown                 "AWS Error SLOW_DOWN during ..."
    connection dropped           "AWS Error NETWORK_CONNECTION during ...: curlCode: 28, Timeout was reached ..."
    403, 404 NoSuchBucket, 429   "AWS Error ACCESS_DENIED", "AWS Error NO_SUCH_BUCKET", "AWS Error UNKNOWN (HTTP status 429)"

S3 answers a request it gave up receiving as a 400 ``RequestTimeout``. Measured 2026-11-22 on pylance 14.0.3 and pyarrow
25.0.0, a proxy in front of moto answering every request with that status and code: pylance flattens it with the store's
body, "... - Server returned non-2xx status code: 400 Bad Request: <?xml ...><Error><Code>RequestTimeout</Code>...", and
pyarrow names the code, "AWS Error REQUEST_TIMEOUT during ListObjectsV2 operation: RequestTimeout". A ``HEAD`` has no
body to carry the code, so no dialect can tell that one from another 400 (pyarrow: "AWS Error UNKNOWN (HTTP status 400)
during HeadObject operation: No response body."; the native backend: ``inner: Status { status: 400, body: Some("") }``),
and it stays the fault a 400 is.

The catalog's control records go through the S3 client (botocore), the one dialect that TYPES its failures: no response
is a ``ConnectionError`` or an ``HTTPClientError`` (no connection, a connect or read past its bound, a connection
closed), and an answer is a ``ClientError`` carrying the status. The library maps them to its own ``StorageError`` family
at one seam, which chains the client's own (``raise ... from exc``), and which of them is an outage is that seam's rule
(``lakehouse.storage.errors.no_answer``), read here on the failure and on each cause beneath it.

The transport failures, the 5xx, a 429 and a ``RequestTimeout`` are outages. A 429 is the store's rate limit: it
answered "not now", which a retry after a wait is for, in every dialect above; a ``RequestTimeout`` is the store giving up
on a request it had not finished receiving, which the next try sends whole. Any other 4xx is NOT: the store answered,
and what it said (no such bucket, access denied, an expired token) will be said again on retry, so it stays the fault it
is rather than becoming a 503 that invites a retry loop against a misconfiguration. The native 403 is excluded twice over: its source is
`PermissionDenied`, not `Generic`, and its status is neither a 5xx nor a 429.
"""

from __future__ import annotations

from lance_namespace import InternalError, ServiceUnavailableError

from lakehouse.storage.errors import no_answer


__all__ = ["reads_as_store_outage", "store_refusal"]

#: Each marker in all three dialects: pylance's flattened `LanceError(IO)` text, the native backend's Debug form, and
#: pyarrow's S3 error. An object-store failure, as opposed to one of Lance's own.
_IO_MARKERS = ("LanceError(IO)", "IO { source: Generic {", "AWS Error ")
#: No response arrived, or its body did not: the connect or the whole request ran out its bound, or the connection dropped.
_TRANSPORT_MARKERS = ("error sending request", "request or response body error", "inner: Http(HttpError", "AWS Error NETWORK_CONNECTION")
#: The store answered with a server error.
_SERVER_ERROR_MARKERS = (
    "non-2xx status code: 5",
    "inner: Status { status: 5",
    "(HTTP status 5",
    "AWS Error INTERNAL_FAILURE",
    "AWS Error SERVICE_UNAVAILABLE",
    "AWS Error SLOW_DOWN",
)
#: The store answered with its rate limit, a 429, in each dialect's words.
_THROTTLED_MARKERS = ("non-2xx status code: 429", "inner: Status { status: 429", "(HTTP status 429)")
#: The store gave up on a request it had not finished receiving: S3's 400 ``RequestTimeout``, by the code in the body
#: pylance and the native backend quote, and by pyarrow's name for it.
_TIMED_OUT_MARKERS = ("<Code>RequestTimeout</Code>", "AWS Error REQUEST_TIMEOUT")
#: Every way the store did not answer the call itself: no response, a server error, its rate limit or its own timeout.
_NO_ANSWER_MARKERS = _TRANSPORT_MARKERS + _SERVER_ERROR_MARKERS + _THROTTLED_MARKERS + _TIMED_OUT_MARKERS


def reads_as_store_outage(exc: BaseException) -> bool:
    """Is this the object store failing to answer, rather than an answer about the target?

    Anything not recognised reads as NOT an outage, so it keeps the status it already had.
    """
    message = str(exc)
    if any(marker in message for marker in _IO_MARKERS) and any(marker in message for marker in _NO_ANSWER_MARKERS):
        return True
    if no_answer(exc):
        return True
    return exc.__cause__ is not None and reads_as_store_outage(exc.__cause__)


def store_refusal(exc: BaseException, *, reading: str, so: str) -> ServiceUnavailableError | InternalError:
    """The spec's refusal for a guard whose ``reading`` failed with ``exc``, saying what it therefore did not do (``so``).

    The store not answering is an outage (503, code 17), which a retry may clear. Anything else, a record that is there
    and cannot be read as one or an answer the store gave, is the target's own state (500, code 18), which only a write
    clears, so it is not told to retry (`lance_docs/ns_catalog/namespace/operations/errors.md`: 17 is "temporarily
    unavailable", 18 an unexpected server error).
    """
    if reads_as_store_outage(exc):
        return ServiceUnavailableError(f"the object store did not answer {reading}, so {so}")
    return InternalError(f"{reading} failed, so {so}")
