"""A location claim's lease and a history fence's hold are timed on the object store's clock, never a pod's ([[LH-413]]).

Every pod reads and writes the same claim and fence records, so a lease stamped with one pod's clock and judged against
another's lapses early or late by exactly the skew between them. Each record is stamped and judged on the store's own
``Date`` (`service_kit.lakehouse.records.StoreRead.at`), so a pod whose clock runs an hour ahead still finds a claim
taken a moment ago inside its lease and a hold taken a moment ago live.

Driven against a moto server, which answers every request with a ``Date`` header as an S3 store does; the second pod's
clock is the only thing changed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, tzinfo

import boto3
import pytest

from service_kit.lakehouse import history_fences, location_claims, records
from service_kit.lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from service_kit.lakehouse.location_claims import ClaimStore, LocationClaim, LocationHeldError
from service_kit.lakehouse.objectfs import StorageOptions


BUCKET = "lh413-control"
LOCATION = "s3://lh413-data/lakehouse/0b9c7a52-3f1e-4d8a-9c6b-2e7f1a0d5c43"
TABLE_UUID = "0b9c7a52-3f1e-4d8a-9c6b-2e7f1a0d5c43"


class _AnHourAhead(datetime):
    """The clock of a pod an hour ahead of the store."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _AnHourAhead:
        ahead = datetime.now(tz) + timedelta(hours=1)
        return cls.fromtimestamp(ahead.timestamp(), tz)


@pytest.fixture
def store(moto_url: str) -> Iterator[StorageOptions]:
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    yield StorageOptions.from_flat({"endpoint": moto_url, "aws_access_key_id": "test", "aws_secret_access_key": "test", "region": "us-east-1"})
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            s3.delete_object(Bucket=BUCKET, Key=item["Key"])
    s3.delete_bucket(Bucket=BUCKET)


def _gone(_claim: LocationClaim) -> bool:
    """``is_gone`` for a holder that no longer resolves to the location: only its lease protects it."""
    return True


def _claim_taken_then_taken_over(storage_options: StorageOptions) -> Callable[[], object]:
    claims = ClaimStore(control_root=f"s3://{BUCKET}/control", storage_options=storage_options)
    location_claims.take(claims, LOCATION, "ns$first", ["ns", "first"], is_gone=_gone, uuid=TABLE_UUID)
    return lambda: location_claims.take(claims, LOCATION, "ns$second", ["ns", "second"], is_gone=_gone)


def _hold_taken_then_another_kind(storage_options: StorageOptions) -> Callable[[], object]:
    history = TableHistory(control_root=f"s3://{BUCKET}/control", storage_options=storage_options, table_uuid=TABLE_UUID)
    history.take(HistoryWork.RECLAIM)
    return lambda: history.take(HistoryWork.REFS)


@pytest.mark.parametrize(
    ("arrange", "refusal"),
    [
        pytest.param(_claim_taken_then_taken_over, LocationHeldError, id="a-claim-inside-its-lease"),
        pytest.param(_hold_taken_then_another_kind, HistoryFencedError, id="a-live-hold"),
    ],
)
def test_a_pod_an_hour_ahead_still_finds_a_fresh_lease_live(
    store: StorageOptions,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[StorageOptions], Callable[[], object]],
    refusal: type[Exception],
) -> None:
    second_pod = arrange(store)
    # Every clock the second pod has, the records layer's included: only the store's `Date` is left to agree with the first.
    for module in (location_claims, history_fences, records):
        monkeypatch.setattr(module, "datetime", _AnHourAhead)

    with pytest.raises(refusal):
        second_pod()
