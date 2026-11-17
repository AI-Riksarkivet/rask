"""A writer's session rewrites a table whose blob column points through an external base, on a real RustFS ([[LH-410]]).

A vend never grants an external blob base: it is a pointer base, whose bytes the blob door serves, and a credential's
unit is a prefix that may hold every tenant's objects ([[LH-209]]). A bring-your-own compaction signs with such a vend
(ADR 0162), so its rewrite has to run without reading through the base: Lance copies the descriptors and never opens
what they point at. moto enforces no session policy, so whether RustFS lets the rewrite's own calls through under the
write tier's policy, while refusing that session the external object itself, is this lane's.

The parent is a user holding the whole data plane, the shape of the catalog's own policy, and the session is minted by
AssumeRole with the policy `catalog.core.vending.build_session_policy` writes for the write tier. The rewrite is planned
and executed and not committed: a stock client's commit is the catalog's to finalize (`version/create`), which the
write tier's policy leaves to it.

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`) against the throwaway RustFS it binds.
"""

from __future__ import annotations

import json
import uuid

import boto3
import lance
import pyarrow as pa
import pytest
from botocore.exceptions import ClientError
from lance.blob import Blob, blob_array
from lance.optimize import Compaction
from pydantic import SecretStr
from throwaway_rustfs import ThrowawayStore, admin, s3_client

from catalog.core.vending import build_session_policy
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options


pytestmark = pytest.mark.rustfs_provisioning

#: The catalog's own canned policy, the whole data plane, which the vend narrows.
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


def test_a_writers_session_rewrites_the_table_and_is_refused_the_objects_its_pointers_name(rustfs_store: ThrowawayStore) -> None:
    suffix = uuid.uuid4().hex[:8]
    warehouse, external = f"wh-{suffix}", f"ext-{suffix}"
    root = s3_client(rustfs_store, rustfs_store.root_access_key, rustfs_store.root_secret())
    for bucket in (warehouse, external):
        root.create_bucket(Bucket=bucket)
    parent, secret, policy = f"catalog{suffix}", uuid.uuid4().hex, f"rask-compact-parent-{suffix}"
    store_admin = admin(rustfs_store)
    store_admin.put("add-canned-policy", {"name": policy}, json.dumps(_DATA_PLANE).encode())
    store_admin.put("add-user", {"accessKey": parent}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    store_admin.put("set-user-or-group-policy", {"policyName": policy, "userOrGroup": parent, "isGroup": "false"}, b"")
    own = lance_storage_options(rustfs_store.endpoint, S3KeyPair(access_key_id=parent, secret_access_key=SecretStr(secret)), "us-east-1").in_clear()
    prefix = f"ns/{uuid.uuid4()}"
    location = f"s3://{warehouse}/{prefix}"
    base = lance.DatasetBasePath(f"s3://{external}/media/", name="media", is_dataset_root=False)
    for page in range(4):
        s3_client(rustfs_store, parent, secret).put_object(Bucket=external, Key=f"media/page-{page}.bin", Body=b"scan")
        rows = pa.table({"id": pa.array([page], pa.int64()), "payload": blob_array([Blob.from_uri(f"s3://{external}/media/page-{page}.bin")])})
        # The base is registered by the create and inherited by each append.
        lance.write_dataset(
            rows,
            location,
            mode="append" if page else "create",
            storage_options=own,
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            initial_bases=None if page else [base],
        )
    sts = boto3.client("sts", endpoint_url=rustfs_store.endpoint, aws_access_key_id=parent, aws_secret_access_key=secret, region_name="us-east-1")
    issued = sts.assume_role(
        RoleArn="arn:aws:iam::123456789012:role/compact",
        RoleSessionName="compact",
        Policy=json.dumps(build_session_policy(warehouse, prefix, "write"), separators=(",", ":")),
        DurationSeconds=900,
    )["Credentials"]
    session = S3KeyPair(
        access_key_id=issued["AccessKeyId"], secret_access_key=SecretStr(issued["SecretAccessKey"]), session_token=SecretStr(issued["SessionToken"])
    )
    vended = lance_storage_options(rustfs_store.endpoint, session, "us-east-1").in_clear()

    table = lance.dataset(location, storage_options=vended)
    rewrites = [task.execute(table) for task in Compaction.plan(table, options={"target_rows_per_fragment": 1024}).tasks]

    assert len(rewrites) == 1, "the four one-row fragments were not planned as one rewrite"
    assert rewrites[0].new_fragments, "the rewrite wrote no fragment"
    writer = s3_client(rustfs_store, issued["AccessKeyId"], issued["SecretAccessKey"], issued["SessionToken"])
    with pytest.raises(ClientError) as refused:
        writer.get_object(Bucket=external, Key="media/page-0.bin")
    assert refused.value.response["Error"]["Code"] == "AccessDenied"
