"""Storage's key listing answers in storage's OWN taxonomy (PS-02).

`storage.errors` exists so that no caller of this package imports botocore: a missing bucket reaches the caller as
:class:`storage.BucketNotFoundError`, not a raw `botocore.exceptions.ClientError` it cannot name.
"""

from __future__ import annotations

import pytest
from moto import mock_aws

from lakehouse.storage import BucketNotFoundError, iter_keys


@mock_aws
def test_iter_keys_over_a_missing_bucket_raises_storages_own_error() -> None:
    import boto3

    with pytest.raises(BucketNotFoundError):
        list(iter_keys(boto3.client("s3", region_name="us-east-1"), "ghost"))
