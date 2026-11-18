"""What a table's maintain-tier vend can write below its history fence is at most a reclaim's hold, for one hold (ADR 0233 §9).

The vend signs a reclaim's hold, so it may put and delete keys shaped as a reclaim's holder below the table's fence
directory (``catalog.core.vending.TableRecords``); an IAM ``?`` matches a ``/`` too, and the body is the writer's. The
catalog's takes trust only the names the fence defines, directly in the directory, a holder's kind by its key
(``lakehouse.fences.HolderFence``, F3 of ``specs/catalog/Fence.tla``). Driven on the lane's real store
(``scripts/lifecycle_on_rustfs_check.sh``) with the REAL vended credential: a non-root user holding a canned policy over the
catalog's bucket assumes a role with the catalog's own maintain session policy, as the vendor does. A mark planted below a
subdirectory by a writer the store does not stop (a store that does not enforce ``?``; here its root) is handed to no
ref door, a reclaim's name whose body is no JSON refuses a ref write as a reclaim does, never as an error, and everything
planted is gone once it has stood a hold. The vend also puts and deletes a cleanup's mark directly in the fence's
directory and a reclaim's holder in the policy fence of each level above its table, and nothing at another level's or
under a policy door's name (ADR 0243 §6).

The vend lasts no longer than a hold (ADR 0243 A1), whatever the vendor's configured TTL: vended through the real door,
it expires within the hold, and the store refuses a delete it signs once it has.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import boto3
import pyarrow as pa
import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from catalog.core.vending import TableRecords, build_session_policy
from lakehouse import history_fences, policy_fences, stored_objects
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from lakehouse.storage_options import StorageOptions
from tests.integration.conftest import RaceCatalog, _vending_session


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
    # F8: the longest bucket name S3 allows, as the control and the data bucket both, and a table at its root as the catalog
    # makes one: the maintain policy at its longest, which the store must take.
    bucket, user, secret = f"vend-{uuid.uuid4().hex}{'v' * 26}", f"catalog{uuid.uuid4().hex[:8]}", secrets.token_urlsafe(24)
    store = _client(*root)
    store.create_bucket(Bucket=bucket)
    canned = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}],
    }
    _admin(root, "add-canned-policy", {"name": user}, json.dumps(canned).encode())
    _admin(root, "add-user", {"accessKey": user}, json.dumps({"secretKey": secret, "status": "enabled"}).encode())
    _admin(root, "set-user-or-group-policy", {"policyName": user, "userOrGroup": user, "isGroup": "false"})
    table_uuid, namespace_uuid, other_namespace = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    records = TableRecords(control_root=f"s3://{bucket}", table_uuid=table_uuid, warehouse="lance_catalog", project="acme")
    policy = build_session_policy(bucket, table_uuid, "maintain", records=records)
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
    level = policy_fences.fence_directory("namespace", namespace_uuid, warehouse="lance_catalog")
    elsewhere = policy_fences.fence_directory("namespace", other_namespace, warehouse="another_warehouse")
    project = policy_fences.fence_directory("project", "acme")
    hex32 = uuid.uuid4().hex
    reach = {
        "a-cleanups-mark": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{fence}/reclaim.{hex32}.json", Body=b"{}")),
        "its-cleanups-mark-removed": _answer(lambda: vend.delete_object(Bucket=bucket, Key=f"{fence}/reclaim.{hex32}.json")),
        "a-reclaim-at-its-namespace": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{level}/reclaim-{hex32}.json", Body=b"{}")),
        "a-reclaim-at-its-project": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{project}/reclaim-{hex32}.json", Body=b"{}")),
        "its-namespaces-holders-listed": _answer(lambda: vend.list_objects_v2(Bucket=bucket, Prefix=f"{level}/")),
        "a-policy-doors-hold-at-its-namespace": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{level}/set-{hex32}.json", Body=b"{}")),
        "a-reclaim-in-another-warehouse": _answer(lambda: vend.put_object(Bucket=bucket, Key=f"{elsewhere}/reclaim-{hex32}.json", Body=b"{}")),
        "another-warehouses-holders-listed": _answer(lambda: vend.list_objects_v2(Bucket=bucket, Prefix=f"{elsewhere}/")),
    }
    for planted in (f"{level}/reclaim-{hex32}.json", f"{project}/reclaim-{hex32}.json"):
        store.delete_object(Bucket=bucket, Key=planted)
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

    assert (vended, reach, refused, marks, left) == (
        {
            "a-reclaim-name-whose-body-is-no-json": "200",
            "a-slash-in-a-reclaim-name": "200",
            "a-mark-below-a-subdirectory": "AccessDenied",
            "a-mark-where-the-catalog-writes-one": "AccessDenied",
        },
        {
            "a-cleanups-mark": "200",
            "its-cleanups-mark-removed": "200",
            "a-reclaim-at-its-namespace": "200",
            "a-reclaim-at-its-project": "200",
            "its-namespaces-holders-listed": "200",
            "a-policy-doors-hold-at-its-namespace": "AccessDenied",
            "a-reclaim-in-another-warehouse": "AccessDenied",
            "another-warehouses-holders-listed": "200",
        },
        "reclaim",
        frozenset(),
        [],
    ), f"the vend's writes answered {vended} and {reach}; a ref write's take was refused by {refused!r}; a reclaim was handed {marks}; {left} stood once lapsed"


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
def test_a_maintain_vend_dies_within_the_history_hold_whatever_the_ttl_configured(race_catalog: RaceCatalog) -> None:
    """A1 of ``specs/maintenance/TablePass.tla``: a pass's credential dies no later than its hold lapses. With the vendor's
    TTL configured at an hour, the maintain tier vended through the real door expires within the history fence's hold, on
    the store's own clock, and the store refuses a delete the credential signs once it has expired. RustFS 1.0.3 issues no
    session shorter than 900 s whatever is asked (measured), so this waits out one hold."""
    from catalog.api.dependencies import get_vendor
    from catalog.core.vending import WebIdentityVendor
    from catalog.main import app
    from lakehouse.store_session import WebIdentity

    identity = WebIdentity(token_file=Path("/nonexistent"), endpoint=_STORE, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/vend")
    vendor = WebIdentityVendor(identity=identity, store_endpoint=_STORE, exchange=_vending_session(), ttl_seconds=3600)
    app.dependency_overrides[get_vendor] = lambda: vendor
    assert requests.post(f"{race_catalog.url}/v1/namespace/lance_catalog$capped/create", json={}, timeout=60).status_code == 200
    created = requests.post(
        f"{race_catalog.url}/v1/table/lance_catalog$capped$t/create?mode=create",
        data=encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())})),
        headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE},
        timeout=60,
    )
    assert created.status_code == 200, created.text
    asked_at = time.time()
    answered = requests.post(f"{race_catalog.url}/management/v1/table/lance_catalog$capped$t/credentials", params={"tier": "maintain"}, timeout=60)
    assert answered.status_code == 200, answered.text
    vended = answered.json()
    lifetime = vended["credentials"]["expires_at_millis"] / 1000 - asked_at
    location = vended["location"].removeprefix("s3://")
    bucket, _, root = location.partition("/")
    options = vended["credentials"]["storage_options"]
    signer = _client(options["aws_access_key_id"], options["aws_secret_access_key"], options["aws_session_token"])
    store = _client(os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"], os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"])
    victim = next(entry["Key"] for entry in store.list_objects_v2(Bucket=bucket, Prefix=f"{root}/data/")["Contents"])
    assert lifetime <= history_fences.HOLD.total_seconds() + 5, f"a maintain vend lasts {lifetime:.0f} s, past the {history_fences.HOLD} hold"
    time.sleep(max(0.0, vended["credentials"]["expires_at_millis"] / 1000 - time.time()) + 5)
    signed = _answer(lambda: signer.delete_object(Bucket=bucket, Key=victim))
    still = _answer(lambda: store.head_object(Bucket=bucket, Key=victim))

    assert (signed in ("ExpiredToken", "AccessDenied", "InvalidAccessKeyId", "InvalidToken"), still) == (True, "200"), (
        f"a delete signed after the vend's expiry answered {signed}, and the file then answers {still}"
    )
