"""[[XC-084]] The drift report lists the store's buckets under the session maintenance's token is exchanged for.

`orphan_buckets` reads every bucket the store holds, and the trash purge runs only on a report that checked every
category, so a listing the store refuses blocks every purge. A session's access key signs only beside its token
(`lance_docs/lance/guide/object_store.md` § S3 Configuration). The store here is a moto server enforcing IAM: it resolves
a session's access key through the token sent with it, so a key sent without its token names no identity and is refused
`InvalidAccessKeyId`, as RustFS refuses it. Its signature check is off, as `services/catalog/tests/conftest.py::two_stores`
explains, and the token exchange is answered unsigned, as AWS answers it. The session is exchanged at moto's STS through
`lakehouse.store_session`, as the deployed service's is, and the report is asked with no bucket client, as its route asks.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import settings as moto_settings
from moto.core.authorization import ActionAuthenticatorMixin
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from lakehouse.store_session import start_store_session
from maintenance.core.config import MaintenanceSettings
from maintenance.services.reconcile import reconcile


#: What maintenance's canned policy lets its own identity read: every bucket's name, and every object.
READ_THE_ESTATE = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListAllMyBuckets"], "Resource": ["arn:aws:s3:::*"]},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject"], "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"]},
    ],
}


class Store(BaseModel):
    """A moto server enforcing IAM, the bucket maintenance sweeps, a bucket nothing claims, and the role its token maps to."""

    url: str
    swept: str
    orphan: str
    role_arn: str


@pytest.fixture
def store() -> Iterator[Store]:
    """The store, with IAM enforced once its buckets and its role exist; per test, since moto's gates are process-global."""
    suffix = uuid.uuid4().hex[:10]
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    clients: dict[str, Any] = {"endpoint_url": url, "aws_access_key_id": "test", "aws_secret_access_key": "test", "region_name": "us-east-1"}
    s3, iam = boto3.client("s3", **clients), boto3.client("iam", **clients)
    swept, orphan = f"swept-{suffix}", f"orphan-{suffix}"
    for bucket in (swept, orphan):
        s3.create_bucket(Bucket=bucket)
    role = f"maintenance-{suffix}"
    iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}))
    iam.put_role_policy(RoleName=role, PolicyName="read-the-estate", PolicyDocument=json.dumps(READ_THE_ESTATE))
    gate = ActionAuthenticatorMixin._authenticate_and_authorize_normal_action

    def _as_aws_gates(self: Any, resource: str = "*") -> None:  # noqa: ANN401 — the moto response class the mixin is mixed into
        if self._get_action() != "AssumeRoleWithWebIdentity":
            gate(self, resource)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(ActionAuthenticatorMixin, "_authenticate_and_authorize_normal_action", _as_aws_gates)
        patch.setattr(moto_settings, "INITIAL_NO_AUTH_ACTION_COUNT", 0)
        yield Store(url=url, swept=swept, orphan=orphan, role_arn=f"arn:aws:iam::123456789012:role/{role}")
    server.stop()


@pytest.mark.asyncio
async def test_the_drift_report_lists_the_stores_buckets_under_the_services_session(store: Store, tmp_path: Path) -> None:
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    settings = MaintenanceSettings(
        s3_bucket=store.swept,
        s3_endpoint=store.url,
        s3_web_identity_token_file=token,
        s3_sts_endpoint=store.url,
        s3_assume_role_arn=store.role_arn,
    )
    session = await start_store_session(settings.web_identity, session_name="maintenance", duration_seconds=settings.s3_session_seconds)
    assert session is not None, "a service projecting a token holds a session"
    try:
        report = await reconcile(settings, warehouses_enabled=False, control_root=f"s3://{store.swept}", namespace_root=f"s3://{store.swept}")
    finally:
        await session.stop()

    unread = [note.reason for note in report.unavailable if note.category == "orphan_buckets"]
    # moto's backends are process-global, so another test's buckets on this worker are listed too.
    found = [orphan.bucket for orphan in report.orphan_buckets if orphan.bucket in {store.swept, store.orphan}]
    assert (unread, found) == ([], [store.orphan])
