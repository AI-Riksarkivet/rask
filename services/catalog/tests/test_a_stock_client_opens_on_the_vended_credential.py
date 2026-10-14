"""[[LH-229]] A stock Lance client opening a table through the namespace signs with the credential the catalog vended.

ADR 0156 C3: the spec doors vend the tier FGA grants the caller, so a stock client never falls back to the
credential its environment holds. The client here is pylance's own ``lance.dataset(namespace_client=...,
table_id=...)`` over ``lance_namespace.connect("rest", ...)``, with no rask import in its construction, against
the real catalog served by uvicorn.

The store is a moto server that enforces IAM, so a key decides what answers. The catalog holds the estate key;
the vendor makes a real STS ``AssumeRole`` against a role that may read the bucket. The client brings its own
credential, a key the store refuses (the control asserts it), as an ambient key outside the governed estate is;
the process environment carries no ``AWS_*`` key and instance metadata is off. So a client that opens the table
signed with what the catalog vended, over what it brought. moto lets an unsigned request through, so a client
with no credential at all would prove nothing here.

moto's signature check is off and its policy check is on, as in ``conftest.two_stores``: measured on moto 5.2.2,
its canonicalisation refuses the object_store client's list request while boto3's identical one passes.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from moto import settings as moto_settings
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from catalog.api.dependencies import get_vendor
from catalog.core.vending import StsVendor
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_REGION = "us-east-1"


class _Store(BaseModel):
    url: str
    bucket: str
    estate_key: str
    estate_secret: str
    role_arn: str


@pytest.fixture
def store() -> Iterator[_Store]:
    suffix = uuid.uuid4().hex[:10]
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    bucket = f"lh229-{suffix}"
    boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name=_REGION).create_bucket(Bucket=bucket)
    iam = boto3.client("iam", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name=_REGION)
    objects = {"Effect": "Allow", "Action": ["s3:*"], "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}
    role = iam.create_role(
        RoleName=f"lance-vend-{suffix}",
        AssumeRolePolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}
        ),
    )["Role"]
    iam.put_role_policy(RoleName=role["RoleName"], PolicyName="table", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [objects]}))
    user = f"estate-{suffix}"
    iam.create_user(UserName=user)
    assume = {"Effect": "Allow", "Action": ["sts:AssumeRole"], "Resource": [role["Arn"]]}
    iam.put_user_policy(UserName=user, PolicyName="estate", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [objects, assume]}))
    key = iam.create_access_key(UserName=user)["AccessKey"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(IAMRequestBase, "check_signature", lambda _self: None)
        patch.setattr(moto_settings, "INITIAL_NO_AUTH_ACTION_COUNT", 0)
        yield _Store(url=url, bucket=bucket, estate_key=key["AccessKeyId"], estate_secret=key["SecretAccessKey"], role_arn=role["Arn"])
    server.stop()


@pytest.fixture
def catalog_url(store: _Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real catalog on the estate key, served by uvicorn, vending through moto's STS; the environment holds no key."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{store.bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{store.bucket}",
        "LANCE_S3_ENDPOINT": store.url,
        "LANCE_S3_ACCESS_KEY_ID": store.estate_key,
        "LANCE_S3_SECRET_ACCESS_KEY": store.estate_secret,
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    for ambient in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(ambient, raising=False)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    vendor = StsVendor(role_arn=store.role_arn, region=_REGION, endpoint=store.url, access_key=store.estate_key, secret_key=store.estate_secret)
    app.dependency_overrides[get_vendor] = lambda: vendor
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=30)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_a_stock_client_opens_a_table_on_the_vended_credential_and_not_its_environment(store: _Store, catalog_url: str) -> None:
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post("/v1/namespace/db/create", json={}).status_code == 200
        rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
        created = catalog.post("/v1/table/db$t/create", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
        location = created.json()["location"]

    # The client's own credential is one the store refuses, as an ambient key outside the governed estate is.
    own = {"aws_endpoint": store.url, "aws_region": _REGION, "allow_http": "true", "aws_access_key_id": "AKIAUNKNOWN", "aws_secret_access_key": "unknown"}
    with pytest.raises(ValueError, match="InvalidAccessKeyId"):
        lance.dataset(location, storage_options=own).count_rows()

    stock = lance_namespace.connect("rest", {"uri": catalog_url})
    dataset = lance.dataset(namespace_client=stock, table_id=["db", "t"], storage_options=own)

    assert dataset.count_rows() == 3
