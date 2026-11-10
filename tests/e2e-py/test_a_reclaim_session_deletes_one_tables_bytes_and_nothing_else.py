"""A trash record's reclaim session lets the purge delete its table's bytes on RustFS, and nothing else ([[XC-084]]).

Maintenance holds no write of its own: the catalog vends it a session for one record, under the policy
`catalog.core.vending.ReclaimScope` writes, and the purge deletes the table's bytes under it; the record and what it keeps
on the control root are then the catalog's to end under its own identity. moto enforces no session policy, so the
catalog's tests judge that policy with an IAM evaluator; whether RustFS's STS lets the purge's own calls through under
it, and refuses one outside it (another table's bytes, the record the catalog judges on, a record an erasure reads), is
this lane's. A session policy is enforced as an intersection with its parent's (measured 2026-10-18, R2), a resource's
trailing `*` matches no empty suffix (`rask-rustfs` § Traps), and `s3:prefix` conditions gate the listings the purge
makes, so a grant that reads right can still miss a call the purge makes.

The parent is a user holding the whole data plane, the shape of the catalog's own policy, and the session is minted by
AssumeRole with the policy the catalog builds; the doors that would hand it over and end the record are stood in for (the
end by `lakehouse.trash.end`, the catalog's own ending, under the parent's pair), as the catalog's judgement of which
record is due is its own tests' claim. The table is written by pylance under a name that needs an escape, so its bytes
and its reservation marker sit under two spellings of its location ([[LH-319]]).

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`) against the throwaway RustFS it binds.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import quote

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, SecretStr
from throwaway_rustfs import ThrowawayStore, admin, s3_client

from catalog.core.vending import ReclaimScope
from lakehouse import trash
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.record_store import put_record, record_key
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services.purge import purge_expired_trash
from maintenance.services.reconcile import CATEGORIES, ReconcileReport


pytestmark = pytest.mark.rustfs_provisioning

CATALOG = "http://catalog.test"
#: The catalog's own canned policy, the whole data plane, which a reclaim session narrows.
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


class _Reclaim(BaseModel):
    """One expired and released trash record on the store, the session vended for it, and what lies beside it."""

    model_config = ConfigDict(frozen=True)

    store: ThrowawayStore
    control: str
    data: str
    #: The table's name, its uuid and its recorded location, spelled as the catalog answers it.
    table: str
    uuid: str
    location: str
    #: Another object's record, which the reclaim must leave.
    other_record: str
    #: The parent's pair, which reads the estate as maintenance's own identity would.
    parent: tuple[str, str]
    #: The session STS issued under the reclaim's policy: its key, secret and token.
    issued: tuple[str, str, str]

    def keys(self, bucket: str) -> set[str]:
        listed = s3_client(self.store, *self.parent).list_objects_v2(Bucket=bucket)
        return {str(item["Key"]) for item in listed.get("Contents", [])}

    def settings(self) -> MaintenanceSettings:
        """Maintenance on the store as its own identity, which reads, with the purge on and the stand-in door to ask."""
        return MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.store.endpoint,
                "s3_access_key_id": self.parent[0],
                "s3_secret_access_key": self.parent[1],
                "s3_bucket": self.control,
                "control_root": f"s3://{self.control}",
                "policy_root": f"s3://{self.control}",
                "trash_purge_enabled": True,
                "catalog_url": CATALOG,
            }
        )


def _options(store: ThrowawayStore, access_key: str, secret_key: str, session_token: str | None = None) -> StorageOptions:
    key_pair = S3KeyPair(access_key_id=access_key, secret_access_key=SecretStr(secret_key), session_token=SecretStr(session_token) if session_token else None)
    return lance_storage_options(store.endpoint, key_pair, "us-east-1")


def _sts(store: ThrowawayStore, access_key: str, secret_key: str):  # noqa: ANN202 — boto3's client type is generated at runtime
    return boto3.client("sts", endpoint_url=store.endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1")


@pytest.fixture
def reclaim(rustfs_store: ThrowawayStore) -> _Reclaim:
    suffix = uuid.uuid4().hex[:8]
    control, data = f"ctl-{suffix}", f"data-{suffix}"
    root = s3_client(rustfs_store, rustfs_store.root_access_key, rustfs_store.root_secret())
    for bucket in (control, data):
        root.create_bucket(Bucket=bucket)
    parent, secret, policy = f"reclaimer{suffix}", uuid.uuid4().hex, f"rask-reclaim-parent-{suffix}"
    store_admin = admin(rustfs_store)
    store_admin.put("add-canned-policy", {"name": policy}, json.dumps(_DATA_PLANE).encode())
    store_admin.put("add-user", {"accessKey": parent}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    store_admin.put("set-user-or-group-policy", {"policyName": policy, "userOrGroup": parent, "isGroup": "false"}, b"")
    own = _options(rustfs_store, parent, secret)
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    name = f"räk{suffix}"
    lance.write_dataset(rows, f"s3://{data}/ns/{name}", storage_options=own.in_clear())
    lance.write_dataset(rows, f"s3://{data}/ns/sibling", storage_options=own.in_clear())
    location = f"s3://{data}/ns/{quote(name)}"
    s3_client(rustfs_store, parent, secret).put_object(Bucket=data, Key=f"ns/{quote(name)}/.lance-reserved", Body=b"")
    control_root, table_uuid, other_uuid = f"s3://{control}", str(uuid.uuid4()), str(uuid.uuid4())
    past = datetime.now(UTC) - timedelta(days=2)
    # Released, as the catalog's release door leaves a record: the purge takes only released records (`purge.due_from`).
    record = trash.make_record(f"ns${name}", uuid=table_uuid, location=location, dropped_by=None, grace_days=1, now=past)
    trash.put(control_root, own, {**record, trash.RELEASED_AT: (datetime.now(UTC) - timedelta(minutes=1)).isoformat()})
    for key in (
        record_key("_protection", "table", table_uuid),
        f"_file_identities/{table_uuid}/txn.json",
        f"_policies/state/{table_uuid}/cadence-x.json",
        record_key("_protection", "table", other_uuid),
    ):
        put_record(control_root, own, key, {"kept": "until the reclaim"})
    scope = ReclaimScope(location=location)
    issued = _sts(rustfs_store, parent, secret).assume_role(
        RoleArn="arn:aws:iam::123456789012:role/reclaim",
        RoleSessionName="reclaim",
        Policy=json.dumps(scope.policy(), separators=(",", ":")),
        DurationSeconds=900,
    )["Credentials"]
    return _Reclaim(
        store=rustfs_store,
        control=control,
        data=data,
        table=f"ns${name}",
        uuid=table_uuid,
        location=location,
        other_record=record_key("_protection", "table", other_uuid),
        parent=(parent, secret),
        issued=(issued["AccessKeyId"], issued["SecretAccessKey"], issued["SessionToken"]),
    )


def _clean_report() -> ReconcileReport:
    return ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts=dict.fromkeys(CATEGORIES, 0),
        total=0,
    )


def test_the_purge_deletes_a_trashed_tables_bytes_under_its_reclaim_session(reclaim: _Reclaim) -> None:
    vended = {"mode": "direct", "credentials": {"storage_options": _options(reclaim.store, *reclaim.issued).in_clear()}}
    parent = _options(reclaim.store, *reclaim.parent)

    def _ended(_request: httpx.Request) -> httpx.Response:
        record = trash.get(f"s3://{reclaim.control}", parent, reclaim.uuid)
        assert record is not None
        trash.end(f"s3://{reclaim.control}", parent, record, kind="table", uuid=reclaim.uuid)
        return httpx.Response(200, json={"tuples_revoked": 0})

    with respx.mock() as door:
        door.post(f"{CATALOG}/management/v1/trash/table/{reclaim.uuid}/credentials").mock(return_value=httpx.Response(200, json=vended))
        door.delete(f"{CATALOG}/management/v1/trash/table/{reclaim.uuid}").mock(side_effect=_ended)
        out = asyncio.run(
            purge_expired_trash(reclaim.settings(), report=_clean_report(), control_root=f"s3://{reclaim.control}", data_roots={f"s3://{reclaim.data}"})
        )

    assert (out.refused, [purged.id for purged in out.purged]) == ([], [reclaim.table]), out
    assert {key for key in reclaim.keys(reclaim.data) if not key.startswith("ns/sibling/")} == set(), "the purge left the table's bytes"
    assert any(key.startswith("ns/sibling/_versions/") for key in reclaim.keys(reclaim.data)), "the purge reached the sibling table"
    assert reclaim.keys(reclaim.control) == {reclaim.other_record}, "the ending left the record's own, or ended another object's"


@pytest.mark.parametrize(
    ("method", "place", "key"),
    [
        pytest.param("delete_object", "data", "ns/sibling/_versions/1.manifest", id="another-tables-bytes"),
        pytest.param("put_object", "control", "trash", id="the-record-the-catalog-judges-on"),
        pytest.param("delete_object", "control", "table-inputs", id="a-record-an-erasure-reads"),
    ],
)
def test_a_reclaim_session_is_refused_a_write_outside_its_tables_bytes(reclaim: _Reclaim, method: str, place: str, key: str) -> None:
    named = {"trash": trash.key(reclaim.uuid), "table-inputs": record_key("_table_inputs", "table", reclaim.uuid)}.get(key, key)
    body = {"Body": b"{}"} if method == "put_object" else {}
    write = partial(getattr(s3_client(reclaim.store, *reclaim.issued), method), Bucket=reclaim.data if place == "data" else reclaim.control, Key=named, **body)

    with pytest.raises(ClientError) as refused:
        write()

    assert refused.value.response["Error"]["Code"] == "AccessDenied"
