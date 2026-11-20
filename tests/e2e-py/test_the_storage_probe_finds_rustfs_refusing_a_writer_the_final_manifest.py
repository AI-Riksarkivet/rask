"""The warehouse storage probe on a real RustFS: a writer's credential stages a manifest and is refused the final one ([[LH-511]]).

Managed versioning (ADR 0157) rests on the store refusing a write-tier credential the final manifest and every ref. The
write tier's policy allows `_versions/*.manifest-*` and denies `*.manifest`, and a staged name ending `.manifest`
matches both, so only a store that puts an explicit Deny above an Allow refuses it. moto enforces no session policy,
and the catalog's own tests stand a store in on both sides of that question; whether RustFS's STS honours the Deny for
the session policy the catalog builds, and lets the staged manifest through under it, is this lane's.

The parent is a user holding the whole data plane, the shape of the catalog's own policy, and each session is minted
by AssumeRole with the policy `catalog.core.vending.build_session_policy` writes for the tier the probe asks, as the
reclaim session's test mints its own.

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`) against the throwaway RustFS it binds.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import boto3
import pytest
from pydantic import SecretStr
from throwaway_rustfs import ThrowawayStore, admin, s3_client

from catalog.api.v1.endpoints.warehouses import _run_scope_probe
from catalog.core.vending import ReclaimScope, SessionTier, TableRecords, VendedSession, build_session_policy, split_s3_location
from catalog.services.vend_probe import summarize_probe
from lakehouse.storage.credential import S3KeyPair


pytestmark = pytest.mark.rustfs_provisioning

#: The catalog's own canned policy, the whole data plane, which each vend narrows.
_DATA_PLANE = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
            "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
        }
    ],
}


class _StsVendor:
    """A `CredentialVendor` on the store's own STS: each vend is a session under the policy the catalog builds for it."""

    def __init__(self, store: ThrowawayStore, access_key: str, secret_key: str) -> None:
        self._store = store
        self._sts = boto3.client("sts", endpoint_url=store.endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1")

    def vend(
        self,
        *,
        table_location: str,
        tier: SessionTier,
        session_name: str,
        bases: Sequence[str] = (),
        data_directories: Sequence[str] = (),
        branch: str = "",
        records: TableRecords | None = None,
    ) -> VendedSession | None:
        bucket, prefix = split_s3_location(table_location)
        policy = build_session_policy(bucket, prefix, tier)
        issued = self._sts.assume_role(
            RoleArn="arn:aws:iam::123456789012:role/probe", RoleSessionName=session_name, Policy=json.dumps(policy, separators=(",", ":")), DurationSeconds=900
        )["Credentials"]
        return VendedSession(
            key_pair=S3KeyPair(
                access_key_id=issued["AccessKeyId"], secret_access_key=SecretStr(issued["SecretAccessKey"]), session_token=SecretStr(issued["SessionToken"])
            ),
            endpoint=self._store.endpoint,
            region="us-east-1",
            expires_at_millis=1,
        )

    def vend_reclaim(self, scope: ReclaimScope, *, session_name: str) -> VendedSession | None:
        raise AssertionError("the storage probe vends no reclaim")


def test_the_probe_finds_rustfs_refusing_a_writer_the_final_manifest_and_every_ref(rustfs_store: ThrowawayStore) -> None:
    suffix = uuid.uuid4().hex[:8]
    bucket, parent, secret, policy = f"wh-{suffix}", f"catalog{suffix}", uuid.uuid4().hex, f"rask-probe-parent-{suffix}"
    s3_client(rustfs_store, rustfs_store.root_access_key, rustfs_store.root_secret()).create_bucket(Bucket=bucket)
    store_admin = admin(rustfs_store)
    store_admin.put("add-canned-policy", {"name": policy}, json.dumps(_DATA_PLANE).encode())
    store_admin.put("add-user", {"accessKey": parent}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    store_admin.put("set-user-or-group-policy", {"policyName": policy, "userOrGroup": parent, "isGroup": "false"}, b"")

    checks = _run_scope_probe(f"s3://{bucket}", vendor=_StsVendor(rustfs_store, parent, secret), store_endpoint=rustfs_store.endpoint, session_name="probe")

    report = summarize_probe(checks)
    assert {check.name: (check.outcome, check.detail) for check in checks if check.outcome != "pass"} == {}
    assert (report.enforced, report.commit_safe, report.versions_guarded) == (True, True, True)
    left = s3_client(rustfs_store, parent, secret).list_objects_v2(Bucket=bucket).get("Contents", [])
    assert [item["Key"] for item in left] == [], "the probe left objects in the warehouse's bucket"
