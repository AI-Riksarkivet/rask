"""[[LH-229]] [[LH-489]] A stock Lance client reaches the store with the credential and the endpoint the catalog vended.

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

The writer runs where the catalog's own store address does not answer it, as an off-cluster client cannot resolve
``rask-minio``: that address is a proxy admitting only requests signed with the estate key, and the catalog vends the
store's address for clients (``LANCE_S3_VENDED_ENDPOINT``). pylance applies the vended ``storage_options`` over the
client's own (measured on pylance 13.0.0), so the endpoint the catalog vends is the one the client writes to.

moto's signature check is off and its policy check is on, as in ``conftest.two_stores``: measured on moto 5.2.2,
its canonicalisation refuses the object_store client's list request while boto3's identical one passes.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from fastapi import FastAPI
from moto import settings as moto_settings
from moto.iam.access_control import IAMRequestBase
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, SecretStr

from catalog.api.dependencies import get_vendor
from catalog.core.vending import StsVendor
from service_kit.governed.store_credential import S3KeyPair
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


def _serve(app: FastAPI) -> Iterator[str]:
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


def _catalog_env(store: _Store, monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
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
        **extra,
    }.items():
        monkeypatch.setenv(key, value)
    for ambient in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(ambient, raising=False)
    from catalog.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def catalog_url(store: _Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real catalog on the estate key, served by uvicorn, vending through moto's STS; the environment holds no key."""
    _catalog_env(store, monkeypatch)
    from catalog.core.config import get_settings
    from catalog.main import app

    def estate() -> S3KeyPair:
        return S3KeyPair(access_key_id=store.estate_key, secret_access_key=SecretStr(store.estate_secret))

    vendor = StsVendor(role_arn=store.role_arn, region=_REGION, endpoint=store.url, credential=estate)
    app.dependency_overrides[get_vendor] = lambda: vendor
    yield from _serve(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


_SIGNER = re.compile(r"Credential=([^/]+)/")


def _estate_only(upstream: str, estate_key: str) -> ThreadingHTTPServer:
    """A forwarding proxy to ``upstream`` that answers 403 to any request not signed with ``estate_key``.

    It is the catalog's in-cluster store address as an off-cluster client meets it: the catalog reaches the store
    through it, and a request signed with anything else, a vended session key among them, does not get through.
    """
    target = urlsplit(upstream)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                out = bytearray()
                while size := int(self.rfile.readline().split(b";")[0], 16):
                    out += self.rfile.read(size)
                    self.rfile.readline()
                self.rfile.readline()
                return bytes(out)
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _forward(self) -> None:
            body = self._body()
            signer = _SIGNER.search(self.headers.get("Authorization", ""))
            if signer is None or signer.group(1) != estate_key:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("transfer-encoding", "content-length", "connection")}
            conn = http.client.HTTPConnection(target.hostname or "", target.port, timeout=60)
            conn.request(self.command, self.path, body=body, headers=headers)
            answer = conn.getresponse()
            payload = answer.read()
            self.send_response(answer.status)
            for k, v in answer.getheaders():
                if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            # A HEAD answers the object's own length, which is the size Lance reads.
            self.send_header("Content-Length", answer.getheader("Content-Length", "0") if self.command == "HEAD" else str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            conn.close()

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _forward

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    return proxy


@pytest.fixture
def off_cluster_catalog_url(store: _Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real catalog reaching the store through an address only the estate key passes, vending the store's own."""
    proxy = _estate_only(store.url, store.estate_key)
    internal = f"http://127.0.0.1:{proxy.server_address[1]}"
    _catalog_env(
        store,
        monkeypatch,
        LANCE_S3_ENDPOINT=internal,
        LANCE_VENDING_MODE="sts",
        LANCE_S3_STS_ENDPOINT=internal,
        LANCE_S3_ASSUME_ROLE_ARN=store.role_arn,
        LANCE_S3_VENDED_ENDPOINT=store.url,
    )
    from catalog.core.config import get_settings
    from catalog.main import app

    yield from _serve(app)
    get_settings.cache_clear()
    proxy.shutdown()


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


def test_a_stock_client_off_the_cluster_creates_and_appends_through_the_endpoint_vended_for_it(store: _Store, off_cluster_catalog_url: str) -> None:
    with httpx.Client(base_url=off_cluster_catalog_url, timeout=60) as catalog:
        assert catalog.post("/v1/namespace/db/create", json={}).status_code == 200

    stock = lance_namespace.connect("rest", {"uri": off_cluster_catalog_url})
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    lance.write_dataset(rows, namespace_client=stock, table_id=["db", "w"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(pa.table({"id": pa.array([3], pa.int64())}), namespace_client=stock, table_id=["db", "w"], mode="append")

    assert lance.dataset(namespace_client=stock, table_id=["db", "w"]).to_table().column("id").to_pylist() == [1, 2, 3]
