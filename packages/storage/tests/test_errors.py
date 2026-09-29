"""The backend-agnostic S3 error taxonomy (`storage.errors`).

Driven against REAL botocore exceptions raised by moto, not hand-built doubles: the
whole point of the module is that it reads the wire-level error codes an S3 backend
actually returns, and a hand-rolled `ClientError` would let a wrong code set pass.
"""

import pytest
from moto import mock_aws

from storage import BucketNotFoundError, StorageError, s3_errors


def _client():
    import boto3

    return boto3.client("s3", region_name="us-east-1")


@mock_aws
def test_head_on_a_missing_bucket_is_always_a_storage_error() -> None:
    """A HEAD has no response body, so WHICH of the two it maps to is backend-dependent.

    moto (like MinIO) still volunteers a `NoSuchBucket` code; AWS answers a bare `404`,
    which is indistinguishable from a missing key and therefore lands on the key branch.
    Pinned as a `StorageError` either way — the taxonomy's contract is "never a raw
    botocore exception", and disambiguating a body-less 404 needs a bucket probe, which is
    the CALLER's job (see `_resolve_missing` in the viewer's objects endpoints).
    """
    client = _client()
    with pytest.raises(StorageError), s3_errors(bucket="ghost", key="k"):
        client.head_object(Bucket="ghost", Key="k")


@mock_aws
def test_get_on_a_missing_bucket_raises_bucket_not_found() -> None:
    """GET does carry a body, so `NoSuchBucket` survives — and beats the key branch."""
    client = _client()
    with pytest.raises(BucketNotFoundError), s3_errors(bucket="ghost", key="k"):
        client.get_object(Bucket="ghost", Key="k")


@mock_aws
def test_a_bare_404_with_no_key_in_scope_is_reported_against_the_bucket() -> None:
    """A listing has no key, so a body-less 404 can only be about the bucket."""
    with pytest.raises(BucketNotFoundError), s3_errors(bucket="b"):
        raise _client_error("404")


def test_non_s3_failures_pass_through_untranslated() -> None:
    """An outage is not a not-found. Translating it would hide a dead endpoint behind a 404."""
    with pytest.raises(ConnectionError), s3_errors(bucket="b", key="k"):
        raise ConnectionError("Could not connect to the endpoint URL")


def _client_error(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")
