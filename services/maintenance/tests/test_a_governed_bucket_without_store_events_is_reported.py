"""The hourly audit asks the store about each governed bucket's event configuration and reports the buckets that send no
store event for their object writes (ADR 0198 §3 and §4).

Over a moto server, which serves S3's notification API: a configured bucket, one whose only rule sends deletes, and a
warehouse's bucket the store cannot answer about (it does not exist), which is unknown and never counted as configured.
The deletes-only bucket holds two warehouses, one rooted at a key prefix of it: the store is asked about the bucket,
once.
The reconciler is stubbed, as the audit's wiring test stubs it: this pins what the audit asks the store, not the drift
report. The same store on RustFS 1.0.3 answers as moto does (measured 2026-11-10, ADR 0198's amendment of that day).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

import boto3
import pytest
from botocore.exceptions import ClientError
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from pydantic import SecretStr

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services import audit, bucket_events
from maintenance.services.reconcile import GrantCensusReader, ReconcileReport, Sources


async def _reconcile(
    settings: MaintenanceSettings,
    grants: GrantCensusReader | None = None,
    *,
    warehouses_enabled: bool,
    control_root: str | None = None,
    namespace_root: str | None = None,
    into_sources: list[Sources] | None = None,
    platform_buckets: Iterable[str] | None = None,
    bucket_client: object = None,
    walk: bool,
) -> ReconcileReport:
    return ReconcileReport(checked_at="2026-11-10T12:00:00+00:00")


class _RefusingStore:
    """The store's client as one whose policy holds no ``s3:GetBucketNotification``: every bucket's read refused 403, as
    maintenance's canned policy without it was refused on the dev estate (ADR 0198's amendment of 2026-11-10)."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def get_bucket_notification_configuration(self, **request: str) -> object:
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied."}}, "GetBucketNotificationConfiguration")

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    ("expected", "refused"),
    [
        pytest.param(True, False, id="events-expected"),
        pytest.param(False, False, id="events-not-expected"),
        pytest.param(True, True, id="every-read-refused"),
    ],
)
def test_the_audit_counts_each_governed_bucket_by_what_the_store_says_of_its_events(
    tmp_path: Path, moto_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, expected: bool, refused: bool
) -> None:
    """Each governed bucket is counted once per audit by outcome: configured, without a rule for every object write, or
    unknown when the store refuses or fails the read. A store refusing every read counts every bucket unknown, never
    configured, and none without."""
    suffix = f"{'expected' if expected else 'unexpected'}{'-refused' if refused else ''}"
    configured, deletes_only, missing = (f"events-{suffix}", f"deletes-{suffix}", f"missing-{suffix}")
    store = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for bucket in (configured, deletes_only):
        store.create_bucket(Bucket=bucket)
    arn = "arn:aws:sqs:us-east-1:123456789012:store-events"
    store.put_bucket_notification_configuration(
        Bucket=configured, NotificationConfiguration={"QueueConfigurations": [{"QueueArn": arn, "Events": ["s3:ObjectCreated:*"]}]}
    )
    store.put_bucket_notification_configuration(
        Bucket=deletes_only, NotificationConfiguration={"QueueConfigurations": [{"QueueArn": arn, "Events": ["s3:ObjectRemoved:*"]}]}
    )
    registry = tmp_path / "_warehouses"
    registry.mkdir()
    (registry / "w1.json").write_text(json.dumps({"id": "w1", "bucket": f"{deletes_only}/data"}))
    (registry / "w2.json").write_text(json.dumps({"id": "w2", "bucket": missing}))
    (registry / "w3.json").write_text(json.dumps({"id": "w3", "bucket": deletes_only}))
    settings = MaintenanceSettings.model_validate(
        {
            "MAINTENANCE_S3_BUCKET": configured,
            "MAINTENANCE_S3_ENDPOINT": moto_url,
            "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
            "MAINTENANCE_S3_SECRET_ACCESS_KEY": SecretStr("test"),
            "MAINTENANCE_CONTROL_ROOT": str(tmp_path),
            "MAINTENANCE_STORE_EVENTS_EXPECTED": expected,
        }
    )
    monkeypatch.setattr(audit, "reconcile", _reconcile)
    if refused:
        real_client = bucket_events.object_client
        monkeypatch.setattr(bucket_events, "object_client", lambda options: _RefusingStore(real_client(options)))
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    counter = provider.get_meter("test").create_counter("maintenance.store_event_buckets.checked")
    monkeypatch.setattr(metrics, "_store_event_buckets_checked", counter)

    with caplog.at_level(logging.WARNING, logger="maintenance.services.bucket_events"):
        out = asyncio.run(audit.run_audit(settings, queue=None))

    warned = sorted((record.getMessage(), getattr(record, "bucket", "")) for record in caplog.records if record.name == "maintenance.services.bucket_events")
    data = reader.get_metrics_data()
    points = cast(Sequence[NumberDataPoint], data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points) if data is not None else ()
    counted = {str((point.attributes or {})["outcome"]): point.value for point in points}
    provider.shutdown()
    if not expected:
        assert ("store_events" in out, warned, counted) == (False, [], {})
        return
    if refused:
        assert out["store_events"] == {"without": [], "unknown": sorted([configured, deletes_only, missing])}
        assert counted == {"unknown": 3}
        return
    assert out["store_events"] == {"without": [deletes_only], "unknown": [missing]}
    assert warned == [("maintenance_bucket_store_events_unknown", missing), ("maintenance_bucket_without_store_events", deletes_only)]
    assert counted == {"configured": 1, "without": 1, "unknown": 1}
