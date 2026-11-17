"""What a table's maintain-tier vend can write below its history fence is at most a reclaim's hold, for one hold (ADR 0233 §9).

The vend signs a reclaim's hold, so it may put and delete keys shaped as a reclaim's holder below the table's fence
directory (``catalog.core.vending.TableRecords``); an IAM ``?`` matches a ``/`` too, and the body is the writer's. The
catalog's takes trust only the names the fence defines, directly in the directory, a holder's kind by its key
(``lakehouse.fences.HolderFence``, F3 of ``specs/catalog/Fence.tla``). Driven on the lane's real store
(``scripts/lifecycle_on_rustfs_check.sh``) with the REAL vended credential: a non-root user holding a canned policy over the
catalog's bucket assumes a role with the catalog's own maintain session policy, as the vendor does. A mark planted below a
subdirectory by a writer the store does not stop (a store that does not enforce ``?``; here its root) is handed to no
ref door, a reclaim's name whose body is no JSON refuses a ref write as a reclaim does, never as an error, and everything
planted is gone once it has stood a hold.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from typing import Any

import boto3
import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from catalog.core.vending import TableRecords, build_session_policy
from lakehouse import history_fences, stored_objects
from lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from lakehouse.storage_options import StorageOptions


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


def test_a_maintain_vend_plants_nothing_a_ref_write_trusts(monkeypatch: pytest.MonkeyPatch) -> None:
    root = (os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    bucket, user, secret = f"vend-{uuid.uuid4().hex[:12]}", f"catalog{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
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
    policy = build_session_policy(bucket, f"data/{table_uuid}", "maintain", records=TableRecords(control_root=f"s3://{bucket}", table_uuid=table_uuid))
    sts = boto3.client("sts", endpoint_url=_STORE, region_name="us-east-1", aws_access_key_id=user, aws_secret_access_key=secret, config=_PATH_STYLE)
    session = sts.assume_role(RoleArn="arn:aws:iam::000000000000:role/vend", RoleSessionName="vend", DurationSeconds=900, Policy=json.dumps(policy))
    vend = _client(session["Credentials"]["AccessKeyId"], session["Credentials"]["SecretAccessKey"], session["Credentials"]["SessionToken"])
    options = StorageOptions.from_flat({"aws_access_key_id": root[0], "aws_secret_access_key": root[1], "aws_region": "us-east-1"}).with_endpoint(_STORE)
    history = TableHistory(control_root=f"s3://{bucket}", storage_options=options, table_uuid=table_uuid)
    fence, mark_id = history.directory, uuid.uuid4().hex

    # What the vend may write: a reclaim's name with any body, and a 32-character name holding a slash; not a mark, where
    # the catalog writes one or below a subdirectory.
    vended = {
        "a-reclaim-name-whose-body-is-no-json": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{fence}/reclaim-{'0' * 32}.json", Body=b"not json")),
        "a-slash-in-a-reclaim-name": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{fence}/reclaim-{'a' * 15}/{'b' * 16}.json", Body=b"{}")),
        "a-mark-below-a-subdirectory": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{fence}/reclaim-x/erasure-release.{mark_id}.json", Body=b"{}")),
        "a-mark-where-the-catalog-writes-one": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{fence}/erasure-release.{mark_id}.json", Body=b"{}")),
    }
    # The same subdirectory mark from a writer the store does not stop, as a store not enforcing `?` would let the vend.
    store.put_object(Bucket=bucket, Key=f"{fence}/reclaim-x/erasure-release.{mark_id}.json", Body=b"{}")
    try:
        history.take(HistoryWork.REFS)
        refused = ""
    except HistoryFencedError as exc:
        refused = str(exc).split()[1]
    with history.hold_for(HistoryWork.RECLAIM, renewed=False) as hold:
        marks = hold.marks
    monkeypatch.setattr(history_fences, "HOLD", timedelta(seconds=2))
    time.sleep(3)
    history.release(history.take(HistoryWork.RECLAIM))
    left = sorted(listed.path.rsplit(f"{fence}/", 1)[-1] for listed in stored_objects.list_under(f"s3://{bucket}/{fence}", options))

    assert (vended, refused, marks, left) == (
        {
            "a-reclaim-name-whose-body-is-no-json": "200",
            "a-slash-in-a-reclaim-name": "200",
            "a-mark-below-a-subdirectory": "AccessDenied",
            "a-mark-where-the-catalog-writes-one": "AccessDenied",
        },
        "reclaim",
        frozenset(),
        [],
    ), f"the vend's writes answered {vended}; a ref write's take was refused by {refused!r}; a reclaim was handed {marks}; {left} stood once lapsed"
