"""No vended credential writes or deletes a pin or a table's condemned versions (ADR 0246, B2 of ``Reclaim.tla``).

A reclaim deletes only what the catalog's batch licensed, and every ref door pins a version, a Lance tag under
``_refs/tags/``, before it reads the record; both are sound only while the catalog's identity alone writes there. Driven
on the lane's real store (``scripts/lifecycle_on_rustfs_check.sh``) with REAL vended credentials: a non-root user
holding a canned policy over the catalog's bucket assumes a role with the catalog's own session policy, as the vendor
does, at the maintain tier and the write tier, at the longest bucket name S3 allows. Each is refused a put and a delete
of a pin, of the table's condemned versions and of a hold of its history fence, while the catalog's pin stands.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
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


@pytest.mark.parametrize("tier", ["maintain", "write"])
def test_a_vended_credential_neither_plants_nor_lifts_a_pin_or_an_entry(tier: str) -> None:
    root = (os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    bucket, user, secret = f"pin-{uuid.uuid4().hex}{'p' * 27}", f"catalog{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
    store = _client(*root)
    store.create_bucket(Bucket=bucket)
    canned = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
    }
    _admin(root, "add-canned-policy", {"name": user}, json.dumps(canned).encode())
    _admin(root, "add-user", {"accessKey": user}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    _admin(root, "set-user-or-group-policy", {"policyName": user, "userOrGroup": user, "isGroup": "false"})
    table_uuid = str(uuid.uuid4())
    records = TableRecords(control_root=f"s3://{bucket}", table_uuid=table_uuid) if tier == "maintain" else None
    policy = build_session_policy(bucket, table_uuid, "maintain" if tier == "maintain" else "write", records=records)
    sts = boto3.client("sts", endpoint_url=_STORE, region_name="us-east-1", aws_access_key_id=user, aws_secret_access_key=secret, config=_PATH_STYLE)
    session = sts.assume_role(RoleArn="arn:aws:iam::000000000000:role/vend", RoleSessionName="vend", DurationSeconds=900, Policy=json.dumps(policy))
    vend = _client(session["Credentials"]["AccessKeyId"], session["Credentials"]["SecretAccessKey"], session["Credentials"]["SessionToken"])
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
    assert counted_length(policy) <= SESSION_POLICY_LIMIT, counted_length(policy)
