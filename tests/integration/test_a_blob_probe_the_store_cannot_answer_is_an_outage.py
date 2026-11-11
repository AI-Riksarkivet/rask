"""A blob probe the store did not answer is an outage, and only a payload that is gone is a dangling column ([[LH-572]] C16).

The publish gate's ``blob_resolves`` assertion reads one byte of the first and last rows' payloads. A failed assertion is
structural: no review waves it through, so it refuses the promotion as a data defect. A store that answered 503 said
nothing about the payload, so the gate raises the outage for the door to answer 503 code 17 and the caller to retry, and
the assertion fails only when the store answered that the payload is not there.

Driven with real pylance on a moto S3 server behind ``store_requests``' proxy: a payload of 200,000 bytes lives in its
fragment's blob sidecar, ``data/<file stem>/<n>.blob`` (measured on pylance 13.0.0), which is what the probe reads.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import lance
import pyarrow as pa
import pytest
from lance_namespace import ServiceUnavailableError
from pydantic import SecretStr

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.quality import BLOB_RESOLVES, assert_quality
from lakehouse.storage_options import StorageOptions
from tests.integration import store_requests


_BUCKET = "blob-probe"
_PAYLOAD = 200_000


@pytest.fixture
def store(moto_url: str) -> Iterator[store_requests.CountedStore]:
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
    with store_requests.counted(moto_url) as counted:
        yield counted


def _with_a_blob_column(store: store_requests.CountedStore, name: str) -> tuple[str, StorageOptions]:
    options = lance_storage_options(store.url, S3KeyPair(access_key_id="k", secret_access_key=SecretStr("s")), "us-east-1")
    uri = f"s3://{_BUCKET}/{name}"
    rows = pa.table({"id": [1, 2], "payload": lance.blob_array([b"x" * _PAYLOAD, b"y" * _PAYLOAD])})
    lance.write_dataset(rows, uri, storage_options=options.in_clear(), data_storage_version="2.2")
    return uri, options


def _is_a_payload(request: store_requests.StoreRequest) -> bool:
    return request.method == "GET" and request.path.endswith(".blob")


def test_a_payload_the_store_did_not_answer_is_raised_as_an_outage(store: store_requests.CountedStore) -> None:
    uri, options = _with_a_blob_column(store, "unanswered")

    with store.answering_unavailable(_is_a_payload), pytest.raises(ServiceUnavailableError, match="payload"):
        assert_quality(uri, options, key_column="")


def test_a_payload_that_is_gone_fails_the_assertion(store: store_requests.CountedStore, moto_url: str) -> None:
    uri, options = _with_a_blob_column(store, "gone")
    seed = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="k", aws_secret_access_key="s", region_name="us-east-1")
    for entry in seed.list_objects_v2(Bucket=_BUCKET, Prefix="gone/data/")["Contents"]:
        if entry["Key"].endswith(".blob"):
            seed.delete_object(Bucket=_BUCKET, Key=entry["Key"])

    assertions = assert_quality(uri, options, key_column="")

    assert [(a.assertion, a.column, a.success) for a in assertions if a.assertion == BLOB_RESOLVES] == [(BLOB_RESOLVES, "payload", False)]
