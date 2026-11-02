"""pyarrow's S3 filesystem reports a store that did not answer in its own dialect, and it reads as the outage it is ([[LH-545]]).

The catalog reads and writes a table's objects through pyarrow (aws-sdk-cpp), so an outage there must be the 503 it is,
not the 500 that says the catalog broke. Each message is pyarrow 25.0.0's own, captured against a store answering the
fault to a HEAD, a multipart PUT or a listing; a 4xx is the store answering, and stays the fault it is.
"""

from __future__ import annotations

import pytest

from service_kit.outage import reads_as_store_outage


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
