"""A record read on an ``s3://`` root whose options name no endpoint signs with the options' key pair ([[LH-572]] C7).

A root naming no endpoint is AWS proper. Every record write reaches it through the object client, signed by the options'
key pair; a read must reach it under the same identity, never the pod's ambient credential chain, or one record is read
and written by two identities, and the read by whichever role the pod happens to hold.

The ambient environment here names a different key pair and points ``AWS_ENDPOINT_URL`` at the store, so a read signed
by the chain reaches it and is seen. A client naming no endpoint addresses the regional endpoint itself, so it reaches the
store through the front ``store_requests.at_aws_proper`` stands for that endpoint: pyarrow's own ``S3FileSystem`` is
handed the front's proxy and CA, and nothing else about the client changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pyarrow.fs as pafs
import pytest
from pydantic import SecretStr

from lakehouse import record_store
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import REGION, StorageOptions
from tests.integration import store_requests


_BUCKET = "records-at-aws"


@pytest.fixture
def store(moto_url: str) -> Iterator[store_requests.CountedStore]:
    """The moto server behind a counting proxy, holding one record under ``_registry/``."""
    seed = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="seed", aws_secret_access_key="seed", region_name="us-east-1")
    seed.create_bucket(Bucket=_BUCKET)
    seed.put_object(Bucket=_BUCKET, Key="_registry/one.json", Body=b'{"name": "one"}')
    with store_requests.counted(moto_url) as counted:
        yield counted


def test_a_record_read_on_a_root_naming_no_endpoint_signs_with_the_options_key_pair(
    store: store_requests.CountedStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "AMBIENT",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "AWS_ENDPOINT_URL": store.url,
        "AWS_ENDPOINT_URL_S3": store.url,
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    options = StorageOptions(key_pair=S3KeyPair(access_key_id="PAIR", secret_access_key=SecretStr("pair-secret")), options={REGION: "us-east-1"})
    filesystem = pafs.S3FileSystem

    with store_requests.at_aws_proper(store) as aws:

        def routed(*args: Any, **kwargs: Any) -> pafs.S3FileSystem:  # noqa: ANN401 — pyarrow's own arguments, every one passed on
            return filesystem(*args, **kwargs, proxy_options=aws.proxy, tls_ca_file_path=aws.ca_file)

        monkeypatch.setattr(pafs, "S3FileSystem", routed)
        record = record_store.get_record(f"s3://{_BUCKET}", options, "_registry/one.json", event="record")

    assert record == {"name": "one"}
    assert {request.access_key for request in store.taken()} == {"PAIR"}
