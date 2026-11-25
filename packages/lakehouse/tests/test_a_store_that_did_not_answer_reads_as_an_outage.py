"""A store that did not answer reads as the outage it is, in pyarrow's dialect and in the S3 client's ([[LH-545]], [[XC-219]]).

The catalog reads and writes a table's objects through pyarrow (aws-sdk-cpp), and its control records through the S3
client (botocore), so an outage in either must be the 503 it is, not the 500 that says the catalog broke. Each pyarrow
message is pyarrow 25.0.0's own, captured against a store answering the fault to a HEAD, a multipart PUT or a listing.
The S3 client types its failures: no response is a ``ConnectionError`` or an ``HTTPClientError``, and an answer is a
``ClientError`` carrying its status. A 4xx is the store answering, and stays the fault it is, except two: a 429, the
store's rate limit, which says "not now", and S3's 400 ``RequestTimeout``, the store giving up on a request it had not
finished receiving. A retry is for both, so each is an outage in every dialect that names it.
"""

import pytest
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ParamValidationError, ReadTimeoutError
from lance_namespace import InternalError

from lakehouse.problems.outage import reads_as_store_outage
from tests.store_answers import answered


_KEY = "When reading information for key 't/_versions/1.manifest' in bucket 'b': "
_LIST = "When listing objects under key 't/_versions/' in bucket 'b': "


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(f"{_LIST}AWS Error SERVICE_UNAVAILABLE during ListObjectsV2 operation: Service is unable to handle request.", id="503"),
        pytest.param(f"{_LIST}AWS Error SLOW_DOWN during ListObjectsV2 operation: Please reduce your request rate.", id="slow-down"),
        pytest.param(f"{_LIST}AWS Error INTERNAL_FAILURE during ListObjectsV2 operation: We encountered an internal error.", id="500"),
        pytest.param(f"{_KEY}AWS Error NETWORK_CONNECTION during HeadObject operation: curlCode: 28, Timeout was reached", id="no-answer-within-the-bound"),
        pytest.param(f"{_LIST}AWS Error REQUEST_TIMEOUT during ListObjectsV2 operation: RequestTimeout", id="a-request-the-store-gave-up-receiving"),
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
        pytest.param(_flattened("400 Bad Request", "RequestTimeout"), True, id="a-request-timeout-on-a-fresh-connection"),
        pytest.param(_native('PermissionDenied { path: "root/__manifest/_versions/1.manifest",', 403), False, id="403-on-an-opened-connection"),
        pytest.param(_flattened("403 Forbidden", "AccessDenied"), False, id="403-on-a-fresh-connection"),
        pytest.param(OSError(f"{_KEY}AWS Error ACCESS_DENIED during HeadObject operation: No response body."), False, id="403-to-pyarrow"),
    ],
)
def test_a_4xx_is_an_outage_only_where_the_store_asks_to_be_asked_again(failure: Exception, outage: bool) -> None:
    """A store's 429 and its 400 ``RequestTimeout`` are the 4xx that are outages: the store asks to be asked again. A 403
    is a refused or expired credential, which the next try meets again. Each native and flattened text is pylance
    14.0.3's, measured 2026-11-22 through a proxy in front of moto answering every request with the status and code
    (`TooManyRequests`, `RequestTimeout`, `AccessDenied`)."""
    assert reads_as_store_outage(failure) is outage


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(EndpointConnectionError(endpoint_url="http://store:9000"), id="no-connection"),
        pytest.param(ReadTimeoutError(endpoint_url="http://store:9000"), id="no-answer-within-the-bound"),
        pytest.param(ConnectionClosedError(endpoint_url="http://store:9000"), id="connection-dropped"),
        pytest.param(answered(500, "InternalError"), id="500"),
        pytest.param(answered(429, "TooManyRequests"), id="429"),
        pytest.param(answered(400, "RequestTimeout"), id="a-request-the-store-gave-up-receiving"),
    ],
)
def test_a_store_the_s3_client_reports_as_not_answering_reads_as_an_outage(failure: Exception) -> None:
    assert reads_as_store_outage(failure)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(answered(403, "AccessDenied"), id="403"),
        pytest.param(answered(501, "NotImplemented"), id="a-capability-the-store-lacks"),
        pytest.param(ParamValidationError(report="Bucket is required"), id="a-request-never-sent"),
    ],
)
def test_an_answer_the_s3_client_reports_is_no_outage(failure: Exception) -> None:
    assert not reads_as_store_outage(failure)


def test_an_outage_a_caller_reworded_still_reads_as_one() -> None:
    """A seam that answers every store failure as an ``OSError`` chains the client's own, which is what says outage."""
    try:
        raise OSError("the store refused recording the author of transaction t") from answered(503, "ServiceUnavailable")
    except OSError as reworded:
        assert reads_as_store_outage(reworded)
