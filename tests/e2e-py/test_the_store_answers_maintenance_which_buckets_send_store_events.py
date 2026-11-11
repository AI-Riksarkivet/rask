"""RustFS answers maintenance's hourly audit which governed buckets send their writes as store events, under the canned
policy the chart renders for maintenance (ADR 0198 §3 and §4).

The audit reads each governed bucket's event configuration (`maintenance.services.bucket_events`): RustFS checks
`s3:GetBucketNotification` on the bucket (measured on 1.0.3, 2026-11-10), so a policy without it is refused 403 on every
bucket and the audit would find every bucket unknown. Here the policy is the one the provisioning hook writes for
maintenance's ServiceAccount, read off the chart's render, bound to a user of the test's own; one bucket is configured as
the development store's fixture configures it (`deploy/fixtures/rustfs`, `bucket-events.yaml`), and one is not.

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`), whose store is booted with a notify target, since
RustFS refuses a bucket's event configuration naming no target it has.
"""

from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr
from throwaway_rustfs import ThrowawayStore, admin, s3_client

from maintenance.core.config import MaintenanceSettings
from maintenance.services.bucket_events import report_bucket_events
from tests.unit.chart_render import DEFAULT_ARGS, render


pytestmark = pytest.mark.rustfs_provisioning

#: What the development store's fixture puts on each bucket.
_FIXTURE_CONFIGURATION = {"QueueConfigurations": [{"Id": "store-events", "QueueArn": "arn:rustfs:sqs::primary:nats", "Events": ["s3:ObjectCreated:*"]}]}


def _maintenance_policy() -> str:
    """The document the provisioning hook writes for maintenance's ServiceAccount, as the render passes it."""
    hook = next(d for d in render(*DEFAULT_ARGS) if d.get("kind") == "Job" and "object-store-provisioning" in d["metadata"]["name"])
    args = hook["spec"]["template"]["spec"]["containers"][0]["args"]
    return next(arg.split("=", 1)[1] for arg in args if arg.startswith("system:serviceaccount:") and arg.split("=", 1)[0].endswith("-sa-maintenance"))


def test_the_store_answers_maintenance_which_buckets_send_store_events(rustfs_store: ThrowawayStore, tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex[:8]
    configured, unconfigured = f"events-on-{suffix}", f"events-off-{suffix}"
    root = s3_client(rustfs_store, rustfs_store.root_access_key, rustfs_store.root_secret())
    for bucket in (configured, unconfigured):
        root.create_bucket(Bucket=bucket)
    root.put_bucket_notification_configuration(Bucket=configured, NotificationConfiguration=_FIXTURE_CONFIGURATION)
    user, secret = f"maintenance-{suffix}", secrets.token_hex(16)
    store_admin = admin(rustfs_store)
    store_admin.put("add-canned-policy", {"name": f"rask-test-maintenance-{suffix}"}, _maintenance_policy().encode())
    store_admin.put("add-user", {"accessKey": user}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    store_admin.put("set-user-or-group-policy", {"policyName": f"rask-test-maintenance-{suffix}", "userOrGroup": user, "isGroup": "false"}, b"")
    (tmp_path / "_warehouses").mkdir()
    (tmp_path / "_warehouses" / "w.json").write_text(json.dumps({"id": "w", "bucket": unconfigured}))
    settings = MaintenanceSettings.model_validate(
        {
            "MAINTENANCE_S3_BUCKET": configured,
            "MAINTENANCE_S3_ENDPOINT": rustfs_store.endpoint,
            "MAINTENANCE_S3_ACCESS_KEY_ID": user,
            "MAINTENANCE_S3_SECRET_ACCESS_KEY": SecretStr(secret),
            "MAINTENANCE_CONTROL_ROOT": str(tmp_path),
        }
    )

    report = report_bucket_events(settings)

    assert report is not None and (report.without, report.unknown) == ([unconfigured], [])
