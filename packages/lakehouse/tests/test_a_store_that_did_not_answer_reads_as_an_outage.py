"""A store that did not answer reads as the outage it is, in pyarrow's dialect and in the S3 client's ([[LH-545]], [[XC-219]]).

The catalog reads and writes a table's objects through pyarrow (aws-sdk-cpp), and its control records through the S3
client (botocore), so an outage in either must be the 503 it is, not the 500 that says the catalog broke. Each pyarrow
message is pyarrow 25.0.0's own, captured against a store answering the fault to a HEAD, a multipart PUT or a listing.
The S3 client types its failures: no response is a ``ConnectionError`` or an ``HTTPClientError``, and an answer is a
``ClientError`` carrying its status. A 4xx is the store answering, and stays the fault it is.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, ParamValidationError, ReadTimeoutError

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


def test_a_4xx_pyarrow_reports_is_the_store_answering_and_no_outage() -> None:
    """A 429 names its status in the same words a 5xx to a HEAD does, so it is the one 4xx a status marker could misread."""
    assert not reads_as_store_outage(OSError(f"{_KEY}AWS Error UNKNOWN (HTTP status 429) during HeadObject operation: No response body."))


def _answered(status: int, code: str) -> ClientError:
    answer: Any = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(answer, "PutObject")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(EndpointConnectionError(endpoint_url="http://store:9000"), id="no-connection"),
        pytest.param(ReadTimeoutError(endpoint_url="http://store:9000"), id="no-answer-within-the-bound"),
        pytest.param(ConnectionClosedError(endpoint_url="http://store:9000"), id="connection-dropped"),
        pytest.param(_answered(503, "SlowDown"), id="503"),
        pytest.param(_answered(500, "InternalError"), id="500"),
    ],
)
def test_a_store_the_s3_client_reports_as_not_answering_reads_as_an_outage(failure: Exception) -> None:
    assert reads_as_store_outage(failure)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_answered(403, "AccessDenied"), id="403"),
        pytest.param(_answered(412, "PreconditionFailed"), id="412"),
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
