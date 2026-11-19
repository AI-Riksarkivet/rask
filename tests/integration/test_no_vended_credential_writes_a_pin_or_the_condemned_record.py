"""No vended credential writes or deletes a pin or a table's condemned versions (ADR 0246, B2 of ``Reclaim.tla``).

A reclaim deletes only what the catalog's batch licensed, and every ref door pins a version, a Lance tag under
``_refs/tags/``, before it reads the record; both are sound only while the catalog's identity alone writes there. Driven
on the lane's real store (``scripts/lifecycle_on_rustfs_check.sh``) with REAL vended credentials: a non-root user
holding a canned policy over the catalog's bucket assumes a role with the catalog's own session policy, as the vendor
does, at the maintain tier and the write tier, at the longest bucket name S3 allows. Each is refused a put and a delete
of a pin, of the table's condemned versions and of a hold of its history fence, while the catalog's pin stands. And a
maintain session dies with the licence it deletes under (assumption C of ``Reclaim.tla``, ADR 0246 §14).
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from catalog.core.vending import SESSION_POLICY_LIMIT, TableRecords, build_session_policy, counted_length
from lakehouse import condemned_versions, history_fences


_STORE = os.environ.get("RASK_LIFECYCLE_STORE_ENDPOINT", "")
_PATH_STYLE = Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1})

pytestmark = pytest.mark.skipif(not _STORE, reason="needs the lane's S3 store (scripts/lifecycle_on_rustfs_check.sh)")


def _client(access: str, secret: str, token: str | None = None) -> Any:  # noqa: ANN401 — a boto3 S3 client
    return boto3.client(
        "s3", endpoint_url=_STORE, region_name="us-east-1", aws_access_key_id=access, aws_secret_access_key=secret, aws_session_token=token, config=_PATH_STYLE
    )


def _admin(root: tuple[str, str], operation: str, query: dict[str, str], body: bytes = b"") -> None:
    """One call of RustFS's admin API, signed with the store's root pair (``scripts/rustfs_admin.py``)."""
    request = AWSRequest(method="PUT", url=f"{_STORE}/rustfs/admin/v3/{operation}", params=query, data=body)
    S3SigV4Auth(Credentials(*root), "s3", "us-east-1").add_auth(request)
    prepared = request.prepare()
    answered = requests.put(prepared.url, headers=dict(prepared.headers), data=body, timeout=30)
    assert answered.status_code == 200, f"{operation}: {answered.status_code} {answered.text[:200]}"


def _answer(call: Any) -> str:  # noqa: ANN401 — a boto3 call
    try:
        call()
    except ClientError as exc:
        return str(exc.response["Error"]["Code"])
    return "200"


def _vended(tier: str, bucket: str, table_uuid: str, *, seconds: int = 900) -> tuple[Any, dict[str, Any]]:
    """A session the store's STS issues, as the vendor mints one, at ``tier`` over the table ``table_uuid`` in ``bucket``
    (made here), to a non-root user holding a canned policy over the bucket: its client, and the session's credentials."""
    root = (os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    user, secret = f"catalog{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
    _client(*root).create_bucket(Bucket=bucket)
    canned = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
    }
    _admin(root, "add-canned-policy", {"name": user}, json.dumps(canned).encode())
    _admin(root, "add-user", {"accessKey": user}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    _admin(root, "set-user-or-group-policy", {"policyName": user, "userOrGroup": user, "isGroup": "false"})
    records = TableRecords(control_root=f"s3://{bucket}", table_uuid=table_uuid) if tier == "maintain" else None
    policy = build_session_policy(bucket, table_uuid, "maintain" if tier == "maintain" else "write", records=records)
    assert counted_length(policy) <= SESSION_POLICY_LIMIT, counted_length(policy)
    sts = boto3.client("sts", endpoint_url=_STORE, region_name="us-east-1", aws_access_key_id=user, aws_secret_access_key=secret, config=_PATH_STYLE)
    session = sts.assume_role(RoleArn="arn:aws:iam::000000000000:role/vend", RoleSessionName="vend", DurationSeconds=seconds, Policy=json.dumps(policy))
    credentials = session["Credentials"]
    return _client(credentials["AccessKeyId"], credentials["SecretAccessKey"], credentials["SessionToken"]), credentials


@pytest.mark.parametrize("tier", ["maintain", "write"])
def test_a_vended_credential_neither_plants_nor_lifts_a_pin_or_an_entry(tier: str) -> None:
    root = (os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    bucket, table_uuid = f"pin-{uuid.uuid4().hex}{'p' * 27}", str(uuid.uuid4())
    store = _client(*root)
    vend, _credentials = _vended(tier, bucket, table_uuid)
    pin = f"{table_uuid}/_refs/tags/{condemned_versions.pin_name()}.json"
    store.put_object(Bucket=bucket, Key=pin, Body=b'{"version": 1}')
    record = condemned_versions.record_key(table_uuid)
    hold = f"{history_fences.fence_directory(table_uuid)}/refs-{'0' * 32}.json"

    answers = {
        "put a pin": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{table_uuid}/_refs/tags/{condemned_versions.pin_name()}.json", Body=b"{}")),
        "delete the catalog's pin": _answer(lambda: vend.delete_object(Bucket=bucket, Key=pin)),
        "put the condemned versions": _answer(lambda: vend.put_object(Bucket=bucket, Key=record, Body=b'{"uuid": "x", "location": "x", "entries": []}')),
        "delete the condemned versions": _answer(lambda: vend.delete_object(Bucket=bucket, Key=record)),
        "put a hold of the history fence": _answer(lambda: vend.put_object(Bucket=bucket, Key=hold, Body=b"{}")),
    }

    assert set(answers.values()) == {"AccessDenied"}, answers
    assert store.get_object(Bucket=bucket, Key=pin)["Body"].read() == b'{"version": 1}', "the catalog's pin did not stand"


@pytest.mark.skipif(os.environ.get("RASK_LIFECYCLE_WAIT_LICENCE") != "1", reason="waits a licence out, about 17 minutes: make lifecycle-rustfs-check-full")
def test_the_store_refuses_a_maintain_session_once_the_licence_it_deletes_under_ends() -> None:
    """C, the clock a licence rests on (ADR 0246 §14): a maintain session minted as the vend door mints one (the catalog's
    maintain session policy, as long as the door's cap ``CREDENTIAL_LIFETIME``) before a batch is asked carries an expiry
    the batch's licence covers (``covers``: the expiry the store stamped, plus the clocks' margin, no later than the
    instant a promotion after it states), and once that instant has passed the store refuses a delete signed by it.

    The fact is the store's (it refuses an expired session); rask's part, that a credential's expiry is no later than the
    licence, is ``covers``, which the lane's other tests and the offline suite pin. RustFS issues no session shorter than
    900 s (measured 2026-11-18), so this waits the licence out, about 17 minutes, and runs only when asked
    (``RASK_LIFECYCLE_WAIT_LICENCE=1``, ``make lifecycle-rustfs-check-full``); its last result is in the ``rask-rustfs``
    skill's measured status and ADR 0246 §15."""
    root = (os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    bucket, table_uuid = f"lic-{uuid.uuid4().hex[:20]}", str(uuid.uuid4())
    vend, credentials = _vended("maintain", bucket, table_uuid, seconds=int(condemned_versions.CREDENTIAL_LIFETIME.total_seconds()))
    licensed_until = datetime.now(UTC) + condemned_versions.LICENCE
    key = f"{table_uuid}/data/a.lance"
    store = _client(*root)
    store.put_object(Bucket=bucket, Key=key, Body=b"bytes")
    licensed = condemned_versions.Verdict(versions=[1], licensed_until=licensed_until)
    assert condemned_versions.covers(licensed, condemned_versions.Credential(dataset=None, expires_at=credentials["Expiration"])), credentials["Expiration"]
    assert _answer(lambda: vend.head_object(Bucket=bucket, Key=key)) == "200", "the session was refused before its licence ended"

    time.sleep(max(0.0, (licensed_until - datetime.now(UTC)).total_seconds()) + 1)
    refused = _answer(lambda: vend.delete_object(Bucket=bucket, Key=key))

    assert refused in {"ExpiredToken", "AccessDenied", "InvalidAccessKeyId", "InvalidToken"}, refused
    assert store.get_object(Bucket=bucket, Key=key)["Body"].read() == b"bytes", "a delete signed past the licence landed"
