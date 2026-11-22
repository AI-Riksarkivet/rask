"""A store that did not answer reads as the outage it is, in pyarrow's dialect and in the S3 client's ([[LH-545]], [[XC-219]]).

The catalog reads and writes a table's objects through pyarrow (aws-sdk-cpp), and its control records through the S3
client (botocore), so an outage in either must be the 503 it is, not the 500 that says the catalog broke. Each pyarrow
message is pyarrow 25.0.0's own, captured against a store answering the fault to a HEAD, a multipart PUT or a listing.
The S3 client types its failures: no response is a ``ConnectionError`` or an ``HTTPClientError``, and an answer is a
``ClientError`` carrying its status. A 4xx is the store answering, and stays the fault it is, except a 429: a rate limit
is the store saying "not now", which a retry is for, so it is an outage in every dialect.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, ParamValidationError, ReadTimeoutError
from lance_namespace import InternalError

from lakehouse.problems.outage import reads_as_store_outage


_KEY = "When reading information for key 't/_versions/1.manifest' in bucket 'b': "
_LIST = "When listing objects under key 't/_versions/' in bucket 'b': "


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(f"{_LIST}AWS Error SERVICE_UNAVAILABLE during ListObjectsV2 operation: Service is unable to handle request.", id="503"),
        pytest.param(f"{_LIST}AWS Error SLOW_DOWN during ListObjectsV2 operation: Please reduce your request rate.", id="slow-down"),
        pytest.param(f"{_LIST}AWS Error INTERNAL_FAILURE during ListObjectsV2 operation: We encountered an internal error.", id="500"),
        pytest.param(f"{_KEY}AWS Error NETWORK_CONNECTION during HeadObject operation: curlCode: 28, Timeout was reached", id="no-answer-within-the-bound"),
    ],
)
def test_a_store_pyarrow_reports_as_not_answering_reads_as_an_outage(message: str) -> None:
    assert reads_as_store_outage(OSError(message))


def _native(source: str, status: int) -> InternalError:
    """The native backend's Debug form of a store's ``status`` to a connection that has opened its index."""
    return InternalError(
        f"Internal error: Failed to check dataset staleness: IO {{ source: {source} source: RetryError(RetryErrorImpl {{ method: HEAD, retries: 0, "
        f'inner: Status {{ status: {status}, body: Some("") }} }}) }} }}'
    )


def _flattened(status: str, code: str) -> OSError:
    """pylance's flattened form of a store's ``status`` to a fresh connection."""
    return OSError(
        "LanceError(IO): Generic S3 error: Error performing list request: Error performing GET http://store/b?list-type=2 in 757.36µs - "
        f"Server returned non-2xx status code: {status}: <Error><Code>{code}</Code></Error>"
    )


@pytest.mark.parametrize(
    ("failure", "outage"),
    [
        pytest.param(_native('Generic { store: "S3",', 429), True, id="429-on-an-opened-connection"),
        pytest.param(_flattened("429 Too Many Requests", "TooManyRequests"), True, id="429-on-a-fresh-connection"),
        pytest.param(OSError(f"{_KEY}AWS Error UNKNOWN (HTTP status 429) during HeadObject operation: No response body."), True, id="429-to-pyarrow"),
        pytest.param(_native('PermissionDenied { path: "root/__manifest/_versions/1.manifest",', 403), False, id="403-on-an-opened-connection"),
        pytest.param(_flattened("403 Forbidden", "AccessDenied"), False, id="403-on-a-fresh-connection"),
        pytest.param(OSError(f"{_KEY}AWS Error ACCESS_DENIED during HeadObject operation: No response body."), False, id="403-to-pyarrow"),
    ],
)
def test_a_429_is_a_rate_limit_and_an_outage_and_a_403_is_the_store_answering(failure: Exception, outage: bool) -> None:
    """A store's 429 is the one 4xx that is an outage: the store asks to be asked again. A 403 is a refused or expired
    credential, which the next try meets again. Each native and flattened text is pylance 14.0.3's, measured 2026-11-22
    through a proxy in front of moto answering every request with the status (`TooManyRequests`, `AccessDenied`)."""
    assert reads_as_store_outage(failure) is outage


def _answered(status: int, code: str) -> ClientError:
    answer: Any = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(answer, "PutObject")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(EndpointConnectionError(endpoint_url="http://store:9000"), id="no-connection"),
        pytest.param(ReadTimeoutError(endpoint_url="http://store:9000"), id="no-answer-within-the-bound"),
        pytest.param(ConnectionClosedError(endpoint_url="http://store:9000"), id="connection-dropped"),
        pytest.param(_answered(500, "InternalError"), id="500"),
        pytest.param(_answered(429, "TooManyRequests"), id="429"),
    ],
)
def test_a_store_the_s3_client_reports_as_not_answering_reads_as_an_outage(failure: Exception) -> None:
    assert reads_as_store_outage(failure)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_answered(403, "AccessDenied"), id="403"),
        pytest.param(_answered(501, "NotImplemented"), id="a-capability-the-store-lacks"),
        pytest.param(ParamValidationError(report="Bucket is required"), id="a-request-never-sent"),
    ],
)
def test_an_answer_the_s3_client_reports_is_no_outage(failure: Exception) -> None:
    assert not reads_as_store_outage(failure)


def test_an_outage_a_caller_reworded_still_reads_as_one() -> None:
    """A seam that answers every store failure as an ``OSError`` chains the client's own, which is what says outage."""
    try:
        raise OSError("the store refused recording the author of transaction t") from _answered(503, "ServiceUnavailable")
    except OSError as reworded:
        assert reads_as_store_outage(reworded)
