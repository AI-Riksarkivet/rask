"""The store's failures in the library's own words (`storage.errors.store_call`).

Driven against real botocore exceptions raised by moto wherever moto answers the case, so a wrong code set cannot pass;
a status moto does not answer (a server error, a 501) is a ``ClientError`` built in the shape botocore parses one into.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ParamValidationError
from moto import mock_aws

from lakehouse.storage.errors import (
    BucketNotFoundError,
    ObjectNotFoundError,
    PreconditionFailedError,
    StorageError,
    StoreRefusedError,
    StoreUnansweredError,
    WriteConflictError,
    store_call,
)


def _get_from_a_missing_bucket(client: Any) -> None:
    client.get_object(Bucket="ghost", Key="k")


def _get_a_missing_key(client: Any) -> None:
    client.create_bucket(Bucket="bkt")
    client.get_object(Bucket="bkt", Key="k")


def _put_over_a_moved_record(client: Any) -> None:
    client.create_bucket(Bucket="bkt")
    client.put_object(Bucket="bkt", Key="k", Body=b"{}")
    client.put_object(Bucket="bkt", Key="k", Body=b"{}", IfMatch='"not-its-etag"')


@pytest.mark.parametrize(
    ("call", "raised"),
    [
        pytest.param(_get_from_a_missing_bucket, BucketNotFoundError, id="no-such-bucket"),
        pytest.param(_get_a_missing_key, ObjectNotFoundError, id="no-such-key"),
        pytest.param(_put_over_a_moved_record, PreconditionFailedError, id="precondition-failed"),
    ],
)
@mock_aws
def test_each_answer_the_store_gives_is_its_own_class_chained_to_the_clients(call: Callable[[Any], None], raised: type[StorageError]) -> None:
    with pytest.raises(StorageError) as caught, store_call("doing it"):
        call(boto3.client("s3", region_name="us-east-1"))

    assert type(caught.value) is raised
    assert isinstance(caught.value.__cause__, ClientError)


def _answered(status: int, code: str) -> ClientError:
    answer: Any = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(answer, "PutObject")


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        pytest.param(_answered(503, "SlowDown"), StoreUnansweredError, id="a-server-error"),
        pytest.param(_answered(501, "NotImplemented"), StoreRefusedError, id="a-capability-the-store-lacks"),
    ],
)
def test_a_server_error_is_no_answer_and_a_501_is_a_refusal(failure: ClientError, raised: type[StorageError]) -> None:
    """The two sides of the outage rule's one exception: a 501 is the store saying it lacks what was asked."""
    with pytest.raises(StorageError) as caught, store_call("writing b/k"):
        raise failure

    assert type(caught.value) is raised
    assert caught.value.__cause__ is failure


def test_a_409_is_a_write_that_met_another_in_flight_whatever_code_names_it() -> None:
    """Neither the condition's verdict (412) nor a refusal: a replace reads it as a record that changed, and a create
    lets it out, since the write it met may yet fail."""
    failure = _answered(409, "OperationAborted")
    with pytest.raises(StorageError) as caught, store_call("replacing b/k"):
        raise failure

    assert type(caught.value) is WriteConflictError
    assert caught.value.__cause__ is failure


def test_no_connection_is_no_answer() -> None:
    failure = EndpointConnectionError(endpoint_url="http://store:9000")
    with pytest.raises(StoreUnansweredError) as caught, store_call("reading b/k"):
        raise failure

    assert caught.value.__cause__ is failure


def test_a_missing_object_is_caught_as_a_file_not_found() -> None:
    """What the catchers written for an absent file on a filesystem already name, the outbox's drop among them."""
    with pytest.raises(FileNotFoundError) as caught, store_call("reading b/k"):
        raise _answered(404, "NoSuchKey")

    assert type(caught.value) is ObjectNotFoundError


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(KeyError("ETag"), id="not-the-clients"),
        pytest.param(ParamValidationError(report="Missing required parameter in input: Bucket"), id="a-call-the-client-refuses-to-make"),
    ],
)
def test_a_failure_that_is_no_answer_of_the_stores_passes_through_as_itself(failure: Exception) -> None:
    """A defect of the call is no ``OSError``: mapped, every ``except OSError`` around it would answer a refusal or an
    outage to retry around, where the defect must reach whoever reads the traceback."""
    with pytest.raises(type(failure)) as caught, store_call("reading b/k"):
        raise failure

    assert caught.value is failure
