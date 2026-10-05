"""ONE answer to "did this Lance call fail because the object store did not answer?".

The sibling of :mod:`service_kit.lancekit.absence`, for the opposite question. Absence PROVES a target
is not there; an outage proves nothing about the target, only that the store could not be asked. The
two must never be confused in either direction: an outage read as absence is how a black-holed store
became "table not found" (404), and an outage read as a broken catalog is how it became a 500 that
sends whoever is on call to the wrong system. 503 is the status that says what happened and asks the
caller to retry.

TEXT, NOT TYPES, for the reason `absence` gives: object_store flattens every failure into a bare
`ValueError`/`OSError` carrying `LanceError(IO)` and the reason in the message. MEASURED on pylance
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

The transport failures and the 5xx are outages. A 4xx is NOT: the store answered, and what it said (no
such bucket, access denied) will be said again on retry, so it stays the fault it is rather than
becoming a 503 that invites a retry loop against a misconfiguration. The native 403 is excluded twice
over: its source is `PermissionDenied`, not `Generic`, and its status is not a 5xx.
"""

from __future__ import annotations


__all__ = ["reads_as_store_outage"]

#: Each marker in both dialects: pylance's flattened `LanceError(IO)` text, and the native backend's Debug form.
#: An object-store failure, as opposed to one of Lance's own.
_IO_MARKERS = ("LanceError(IO)", "IO { source: Generic {")
#: No response arrived, or its body did not: the connect or the whole request ran out its bound, or the connection dropped.
_TRANSPORT_MARKERS = ("error sending request", "request or response body error", "inner: Http(HttpError")
#: The store answered with a server error.
_SERVER_ERROR_MARKERS = ("non-2xx status code: 5", "inner: Status { status: 5")


def reads_as_store_outage(exc: BaseException) -> bool:
    """Is this the object store failing to answer, rather than an answer about the target?

    Anything not recognised reads as NOT an outage, so it keeps the status it already had.
    """
    message = str(exc)
    if not any(marker in message for marker in _IO_MARKERS):
        return False
    return any(marker in message for marker in _TRANSPORT_MARKERS + _SERVER_ERROR_MARKERS)
