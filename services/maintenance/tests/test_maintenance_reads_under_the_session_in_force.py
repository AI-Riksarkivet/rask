"""[[XC-084]] Maintenance reads the store under the session its token is exchanged for, the one in force at each read.

A session's access key signs only beside its token (`lance_docs/lance/guide/object_store.md` § S3 Configuration). The
store here is a moto server enforcing IAM: it resolves a session's access key through the token sent with it, so a key
sent without its token names no identity and is refused `InvalidAccessKeyId`, as RustFS refuses it, and so is a session
the store no longer holds, as RustFS refuses an expired one. Its signature check is off, as
`services/catalog/tests/conftest.py::two_stores` explains, and the token exchange is answered unsigned, as AWS answers
it. The session is exchanged at moto's STS through `lakehouse.store_session`, as the deployed service's is.

* The drift report lists the store's buckets, as its route asks, with no bucket client: `orphan_buckets` reads every
  bucket the store holds, and the trash purge runs only on a report that checked every category, so a listing the store
  refuses blocks every purge.
* A replica handles a store event under the session in force when the event arrives, not the one held when the replica
  started: a session lives an hour, and a claim read signed with an expired one would hold no table's pass for the rest
  of the replica's life ([[LH-494]]).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
from moto import settings as moto_settings
from moto.core import DEFAULT_ACCOUNT_ID
from moto.core.authorization import ActionAuthenticatorMixin
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from moto.sts.models import sts_backends
from pydantic import BaseModel, SecretStr

from lakehouse import location_claims, store_session
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.store_session import SessionCache, start_store_session
from maintenance.core.config import MaintenanceSettings
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.reconcile import reconcile
from maintenance.services.store_events import StoreEvents


#: What maintenance's canned policy lets its own identity read: every bucket's name, and every object.
READ_THE_ESTATE = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListAllMyBuckets"], "Resource": ["arn:aws:s3:::*"]},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject"], "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"]},
    ],
}


#: What the rig's own user may do, which seeds what the catalog would have written.
DO_ANYTHING = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}


class Store(BaseModel):
    """A moto server enforcing IAM, the bucket maintenance sweeps, a bucket nothing claims, the role its token maps to, and
    the access key of the rig's own user."""

    url: str
    swept: str
    orphan: str
    role_arn: str
    rig_key: str


@pytest.fixture
def store() -> Iterator[Store]:
    """The store, with IAM enforced once its buckets, its role and the rig's user exist; per test, since moto's gates are
    process-global."""
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
    rig = f"rig-{suffix}"
    iam.create_user(UserName=rig)
    iam.put_user_policy(UserName=rig, PolicyName="do-anything", PolicyDocument=json.dumps(DO_ANYTHING))
    rig_key = iam.create_access_key(UserName=rig)["AccessKey"]["AccessKeyId"]
    gate = ActionAuthenticatorMixin._authenticate_and_authorize_normal_action

    def _as_aws_gates(self: Any, resource: str = "*") -> None:  # noqa: ANN401 — the moto response class the mixin is mixed into
        if self._get_action() != "AssumeRoleWithWebIdentity":
            gate(self, resource)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(ActionAuthenticatorMixin, "_authenticate_and_authorize_normal_action", _as_aws_gates)
        patch.setattr(moto_settings, "INITIAL_NO_AUTH_ACTION_COUNT", 0)
        yield Store(url=url, swept=swept, orphan=orphan, role_arn=f"arn:aws:iam::123456789012:role/{role}", rig_key=rig_key)
    server.stop()


def _settings(store: Store, tmp_path: Path) -> MaintenanceSettings:
    """Maintenance's settings on ``store``: its token projected, exchanged at the store's STS for the role."""
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    return MaintenanceSettings(
        s3_bucket=store.swept,
        s3_endpoint=store.url,
        s3_web_identity_token_file=token,
        s3_sts_endpoint=store.url,
        s3_assume_role_arn=store.role_arn,
    )


class _Clock:
    """The session cache's clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FollowUps:
    """The follow-ups a store event holds, with `TaskQueue.follow_up`'s signature."""

    def __init__(self) -> None:
        self.held: list[Task] = []

    async def follow_up(self, task: Task, at: datetime) -> bool:
        self.held.append(task)
        return True


@pytest.mark.asyncio
async def test_the_drift_report_lists_the_stores_buckets_under_the_services_session(store: Store, tmp_path: Path) -> None:
    settings = _settings(store, tmp_path)
    session = await start_store_session(settings.web_identity, session_name="maintenance", duration_seconds=settings.s3_session_seconds)
    assert session is not None, "a service projecting a token holds a session"
    try:
        report = await reconcile(settings, warehouses_enabled=False, control_root=f"s3://{store.swept}", namespace_root=f"s3://{store.swept}", walk=False)
    finally:
        await session.stop()

    unread = [note.reason for note in report.unavailable if note.category == "orphan_buckets"]
    # moto's backends are process-global, so another test's buckets on this worker are listed too.
    found = [orphan.bucket for orphan in report.orphan_buckets if orphan.bucket in {store.swept, store.orphan}]
    assert (unread, found) == ([], [store.orphan])


@pytest.mark.asyncio
async def test_a_store_event_reads_its_tables_claim_under_the_session_in_force(store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(store, tmp_path)
    directory, table_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    location = f"s3://{store.swept}/{directory}"
    rig = lance_storage_options(store.url, S3KeyPair(access_key_id=store.rig_key, secret_access_key=SecretStr("rig")), "us-east-1")
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options=rig)
    location_claims.take(claims, location, "db$t", ["db", "t"], is_gone=lambda _claim: False, uuid=table_uuid)
    clock = _Clock()
    monkeypatch.setattr(store_session, "SESSIONS", SessionCache(clock=clock))
    identity = settings.web_identity
    assert identity is not None, "a service projecting a token has a web identity"
    session = await start_store_session(identity, session_name="maintenance", duration_seconds=settings.s3_session_seconds)
    assert session is not None, "a service projecting a token holds a session"
    follow_ups = _FollowUps()
    try:
        replica = StoreEvents(settings, cast(TaskQueue, follow_ups))
        first = store_session.held_session(identity).access_key_id
        clock.now = settings.s3_session_seconds / 2 + 1
        assert session.refresh_if_due(), "half the session's life is used, so it is exchanged again"
        backend = sts_backends[DEFAULT_ACCOUNT_ID]["aws"]
        backend.assumed_roles = [role for role in backend.assumed_roles if role.access_key_id != first]
        event = {"Records": [{"eventName": "s3:ObjectCreated:Put", "s3": {"bucket": {"name": store.swept}, "object": {"key": f"{directory}/data/x.lance"}}}]}

        held = await replica.hold_passes(json.dumps(event).encode())
    finally:
        await session.stop()

    assert (held, follow_ups.held) == (1, [Task(kind="table", uuid=table_uuid, location=location)])
