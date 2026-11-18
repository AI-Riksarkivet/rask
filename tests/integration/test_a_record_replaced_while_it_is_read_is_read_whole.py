"""A record replaced while it is read is read whole.

A record's read is one request to the store. A read that sizes the object with one request and fetches that many bytes
with a second reads a record replaced in between as the new bytes cut to the old length: no record at all, which a
tolerant listing skips and a strict one raises on, though every record in the store is whole.

Driven on a real S3 store (moto) behind the forwarding proxy: the listing's fetch of the record is held, the record is
replaced by a longer one, and the fetch is let go.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from pydantic import SecretStr

from lakehouse import record_store
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import REGION, StorageOptions
from tests.integration import store_requests


_BUCKET = "records-replaced"
_KEY = "_registry/one.json"
WAIT = 30.0


@pytest.fixture
def store(moto_url: str) -> Iterator[tuple[store_requests.CountedStore, Any]]:
    """The moto server behind the forwarding proxy, holding one record, and a client that reaches the store past it."""
    direct = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="seed", aws_secret_access_key="seed", region_name="us-east-1")
    direct.create_bucket(Bucket=_BUCKET)
    direct.put_object(Bucket=_BUCKET, Key=_KEY, Body=b'{"name": "one"}')
    with store_requests.counted(moto_url) as counted:
        yield counted, direct


def test_a_record_replaced_while_a_strict_listing_reads_it_is_read_whole(store: tuple[store_requests.CountedStore, Any]) -> None:
    proxy, direct = store
    options = StorageOptions(key_pair=S3KeyPair(access_key_id="PAIR", secret_access_key=SecretStr("pair-secret")), options={REGION: "us-east-1"})
    options = options.with_endpoint(proxy.url)
    listed: list[Any] = []

    def listing() -> None:
        try:
            listed.append(record_store.list_records(f"s3://{_BUCKET}", options, "_registry", event="registry", strict=True))
        except Exception as exc:  # noqa: BLE001 — what the listing raised is what the test reports
            listed.append(exc)

    with proxy.holding(lambda request: request.method == "GET" and request.path.endswith(_KEY)) as held:
        reader = threading.Thread(target=listing)
        reader.start()
        assert held.reached(WAIT), "the listing never fetched the record"
        direct.put_object(Bucket=_BUCKET, Key=_KEY, Body=b'{"name": "one", "swept_at": "2026-11-18T22:00:56+00:00"}')
        held.let_go()
        reader.join(WAIT)

    assert listed == [[{"name": "one", "swept_at": "2026-11-18T22:00:56+00:00"}]]
