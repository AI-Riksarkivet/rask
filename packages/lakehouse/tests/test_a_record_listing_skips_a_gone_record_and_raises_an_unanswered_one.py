"""A listing of control records (`lakehouse.records.record_store`), read through a real S3 store: a record deleted
between the listing that names it and its read is no record, and a read the store does not answer fails the listing
whether its caller tolerates an unreadable record or not, since one skipped as unreadable would read as a corrupt
record, which no retry mends ([[LH-572]])."""

import json
import uuid
from collections.abc import Callable
from typing import Any

import boto3
import pytest
from pydantic import SecretStr

from lakehouse.records import record_store
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.objectfs import lance_storage_options
from lakehouse.storage.options import StorageOptions
from tests.integration.store_requests import StoreRequest, counted


PREFIX = "_things"

type Listing = Callable[..., list[dict[str, Any]]]


def _stored(moto_url: str, *names: str) -> str:
    """A bucket of its own holding one record per name under :data:`PREFIX`; answers its control root."""
    bucket = f"listing-{uuid.uuid4().hex[:8]}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    for name in names:
        s3.put_object(Bucket=bucket, Key=f"{PREFIX}/{name}.json", Body=json.dumps({"name": name}).encode())
    return f"s3://{bucket}"


def _options(store_url: str) -> StorageOptions:
    return lance_storage_options(store_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")


def _reads(name: str) -> Callable[[StoreRequest], bool]:
    """Whether a request reads the record ``name``: its HEAD or its GET, never the listing that names it."""
    return lambda request: request.method in ("HEAD", "GET") and request.path.endswith(f"/{PREFIX}/{name}.json")


def test_a_record_deleted_after_the_listing_named_it_is_no_record(moto_url: str) -> None:
    control_root = _stored(moto_url, "a", "b", "c")

    with counted(moto_url) as store, store.answering_absent(_reads("b")):
        listed = record_store.list_every_record(control_root, _options(store.url), PREFIX, event="thing")

    assert sorted(record["name"] for record in listed) == ["a", "c"]


@pytest.mark.parametrize("listing", [pytest.param(record_store.list_records, id="tolerant"), pytest.param(record_store.list_every_record, id="strict")])
def test_a_record_the_store_does_not_answer_fails_the_listing(moto_url: str, listing: Listing) -> None:
    control_root = _stored(moto_url, "a", "b", "c")

    with counted(moto_url) as store, store.answering_unavailable(_reads("b")), pytest.raises(OSError):
        listing(control_root, _options(store.url), PREFIX, event="thing")
