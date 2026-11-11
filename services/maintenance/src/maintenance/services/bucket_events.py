"""Whether each governed bucket is configured to send its object writes as store events (ADR 0198 §3 and §4).

The store's operator configures each rask bucket to deliver every object write to the bus (``objectStore.events``), and
a production install attests it (``objectStore.events.provisioned``). The attestation is a value someone set once,
while a warehouse bucket the catalog creates afterwards is configured, or not, by the operator's own means. So the
hourly audit asks the store, for the bucket of every root a table's pass governs (the configured roots and every active
warehouse's, :func:`maintenance.services.table_pass.governs`), for the bucket's event configuration
(S3 ``GetBucketNotificationConfiguration``), and reports each one holding no rule for every object write. A governed
root may carry a key prefix (``bucket/prefix``), and several may share a bucket; the configuration is the bucket's, so
each bucket is asked about once, by its own name.

What the answer proves, measured on RustFS 1.0.3 (2026-11-10): the store answers the configuration as it was put,
an empty one for a bucket configured with none, and checks the action ``s3:GetBucketNotification`` against the bucket's
own resource (``s3:GetBucketNotificationConfiguration`` is refused as no action at all). It does not prove delivery: the
store accepted a rule naming a target it does not have (``arn:rustfs:sqs::nosuch:nats``) and answers it back as
configured, and the target's own bus may be down. A bucket the store will not answer about is unknown, logged, counted as
unknown, and never counted as configured.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field

from lakehouse import warehouse_records
from lakehouse.governed.store_credential import StoreCredentialUnavailableError
from lakehouse.storage_options import object_client
from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import record_store_event_bucket_checked


log = logging.getLogger(__name__)

#: The event every rule must cover: each object write, a multipart upload's completion and a copy included.
_EVERY_WRITE: Final = "s3:ObjectCreated:*"
#: The configuration's rule lists, one per kind of target.
_RULE_LISTS: Final = ("QueueConfigurations", "TopicConfigurations", "LambdaFunctionConfigurations")


class BucketEventsReport(BaseModel):
    """What the audit found: the governed buckets with no rule for every object write, and those it could not ask about."""

    without: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)


def covers_every_write(configuration: dict[str, Any]) -> bool:
    """Whether ``configuration`` (a GetBucketNotificationConfiguration answer) holds a rule sending every object write
    of the whole bucket: ``s3:ObjectCreated:*``, with no key filter narrowing it to part of the bucket."""
    for kind in _RULE_LISTS:
        for rule in configuration.get(kind) or []:
            filtered = ((rule.get("Filter") or {}).get("Key") or {}).get("FilterRules")
            if _EVERY_WRITE in (rule.get("Events") or []) and not filtered:
                return True
    return False


def report_bucket_events(settings: MaintenanceSettings) -> BucketEventsReport | None:
    """Ask the store about every governed bucket's event configuration, log each bucket with no rule for every object
    write (``maintenance_bucket_without_store_events``, WARNING) and each it could not ask about
    (``maintenance_bucket_store_events_unknown``, WARNING), and count each bucket on
    ``maintenance.store_event_buckets.checked`` by outcome: ``configured``, ``without`` or ``unknown``. A store refusing
    the read for every bucket counts every one unknown, which pages as surely as one missing its events.

    None, counting nothing, when the governed set itself cannot be read (the warehouse registry or the store session):
    a count over part of the set would read as fewer buckets missing their events.
    """
    try:
        options = settings.storage_options()
        records = warehouse_records.list_warehouse_records(settings.resolved_control_root, options)
    except (OSError, ValueError, ClientError, BotoCoreError, StoreCredentialUnavailableError) as exc:
        log.warning("maintenance_bucket_store_events_unchecked", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None
    roots = (*settings.sweep_buckets, *warehouse_records.maintainable_buckets(records))
    buckets = sorted({root.partition("/")[0] for root in roots})
    client = object_client(options)
    report = BucketEventsReport()
    for bucket in buckets:
        try:
            configuration = client.get_bucket_notification_configuration(Bucket=bucket)
        except (ClientError, BotoCoreError) as exc:
            log.warning("maintenance_bucket_store_events_unknown", extra={"bucket": bucket, "error": f"{type(exc).__name__}: {exc}"[:300]})
            report.unknown.append(bucket)
            record_store_event_bucket_checked(outcome="unknown")
            continue
        if not covers_every_write(configuration):
            log.warning("maintenance_bucket_without_store_events", extra={"bucket": bucket})
            report.without.append(bucket)
            record_store_event_bucket_checked(outcome="without")
            continue
        record_store_event_bucket_checked(outcome="configured")
    return report
