"""[[LH-229]] [[LH-489]] A stock Lance client reaches the store with the credential and the endpoint the catalog vended.

ADR 0156 C3: the spec doors vend the tier FGA grants the caller, so a stock client never falls back to the
credential its environment holds. The client here is pylance's own ``lance.dataset(namespace_client=...,
table_id=...)`` over ``lance_namespace.connect("rest", ...)``, with no rask import in its construction, against
the real catalog served by uvicorn.

The store is a moto server that enforces IAM (``conftest.two_stores``), so a key decides what answers. The catalog
holds no key: its projected token is exchanged on moto's STS for sessions under a role that may read the bucket (ADR
0170), its own and each vend's. The client brings its own credential, a key the store refuses (the control asserts
it), as an ambient key outside the governed estate is; the process environment carries no ``AWS_*`` key and instance
metadata is off. So a client that opens the table signed with what the catalog vended, over what it brought. moto lets
an unsigned request through, so a client with no credential at all would prove nothing here.

The writer runs where the catalog's own store address does not answer it, as an off-cluster client cannot resolve
``rask-rustfs``: that address is a proxy admitting only requests signed with the catalog's own session, and the catalog
vends the store's address for clients (``LANCE_S3_VENDED_ENDPOINT``). pylance applies the vended ``storage_options``
over the client's own (measured on pylance 13.0.0), so the endpoint the catalog vends is the one the client writes to.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from fastapi import FastAPI
from moto.iam.access_control import IAMPolicy, PermissionResult
from moto.sts.models import sts_backends
from pydantic import BaseModel

from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_REGION = "us-east-1"
#: The account moto files every role and session under.
_ACCOUNT = "123456789012"


class _Store(BaseModel):
    url: str
    bucket: str
    role_arn: str
    token_file: str


@pytest.fixture
def store(two_stores: Any, tmp_path: Path) -> _Store:  # noqa: ANN401 — conftest.TwoStores
    """The estate bucket, a role allowed it that this test's catalog's token is exchanged for, and the projected token."""
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    return _Store(url=two_stores.url, bucket=two_stores.estate_bucket, role_arn=two_stores.role_on(two_stores.estate_bucket), token_file=str(token))


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
        "LANCE_S3_WEB_IDENTITY_TOKEN_FILE": store.token_file,
        "LANCE_S3_STS_ENDPOINT": store.url,
        "LANCE_S3_ASSUME_ROLE_ARN": store.role_arn,
        "LANCE_VENDING_MODE": "web_identity",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        "AWS_EC2_METADATA_DISABLED": "true",
        **extra,
    }.items():
        monkeypatch.setenv(key, value)
    for ambient in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "LANCE_S3_ACCESS_KEY_ID", "LANCE_S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(ambient, raising=False)
    from catalog.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def catalog_url(store: _Store, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The real catalog on its token's session, served by uvicorn, vending through moto's STS; the environment holds no key."""
    _catalog_env(store, monkeypatch)
    from catalog.core.config import get_settings
    from catalog.main import app

    yield from _serve(app)
    get_settings.cache_clear()


_SIGNER = re.compile(r"Credential=([^/]+)/")


def _catalogs_own(role_arn: str) -> Callable[[str], bool]:
    """Whether an access key is a session the catalog exchanged for its own IO under ``role_arn``, by moto's record."""

    def _owned(key: str) -> bool:
        return any(
            role.access_key_id == key and role.role_arn == role_arn and role.session_name == "catalog" for role in sts_backends[_ACCOUNT]["aws"].assumed_roles
        )

    return _owned


def _catalog_only(upstream: str, admits: Callable[[str], bool]) -> ThreadingHTTPServer:
    """A forwarding proxy to ``upstream`` that answers 403 to any request not signed with a key ``admits``.

    It is the catalog's in-cluster store address as an off-cluster client meets it: the catalog reaches the store
    through it with its own session, and a request signed with anything else, a vended session among them, does not
    get through.
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
            if signer is None or not admits(signer.group(1)):
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
    """The real catalog reaching the store through an address only its own session passes, vending the store's own, and
    addressed under its public prefix, as the platform's Gateway forwards an off-cluster client's request unrewritten."""
    proxy = _catalog_only(store.url, _catalogs_own(store.role_arn))
    internal = f"http://127.0.0.1:{proxy.server_address[1]}"
    _catalog_env(store, monkeypatch, LANCE_S3_ENDPOINT=internal, LANCE_S3_VENDED_ENDPOINT=store.url, LANCE_PUBLIC_PATH_PREFIX="/api/catalog")
    from catalog.core.config import get_settings
    from catalog.main import app

    for served in _serve(app):
        yield f"{served}/api/catalog"
    get_settings.cache_clear()
    proxy.shutdown()


def test_a_stock_client_opens_a_table_on_the_vended_credential_and_not_its_environment(store: _Store, catalog_url: str) -> None:
    namespace = f"db{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
        created = catalog.post(f"/v1/table/{namespace}$t/create", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
        location = created.json()["location"]

    # The client's own credential is one the store refuses, as an ambient key outside the governed estate is.
    own = {"aws_endpoint": store.url, "aws_region": _REGION, "allow_http": "true", "aws_access_key_id": "AKIAUNKNOWN", "aws_secret_access_key": "unknown"}
    with pytest.raises(ValueError, match="InvalidAccessKeyId"):
        lance.dataset(location, storage_options=own).count_rows()

    stock = lance_namespace.connect("rest", {"uri": catalog_url})
    dataset = lance.dataset(namespace_client=stock, table_id=[namespace, "t"], storage_options=own)

    assert dataset.count_rows() == 3


def test_a_stock_client_off_the_cluster_creates_and_appends_through_the_endpoint_vended_for_it(store: _Store, off_cluster_catalog_url: str) -> None:
    """The table is named `sp ace`, which pylance's Rust client sends as `sp+ace` ([[LH-526]], ADR 0173 §4): it creates,
    appends to and reads the table the name names, the one another client finds sending the space as `%20`. Both reach
    the catalog under its public prefix, which the catalog strips from the bytes as sent."""
    namespace = f"db{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=off_cluster_catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200

    stock = lance_namespace.connect("rest", {"uri": off_cluster_catalog_url})
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    lance.write_dataset(rows, namespace_client=stock, table_id=[namespace, "sp ace"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(pa.table({"id": pa.array([3], pa.int64())}), namespace_client=stock, table_id=[namespace, "sp ace"], mode="append")

    assert lance.dataset(namespace_client=stock, table_id=[namespace, "sp ace"]).to_table().column("id").to_pylist() == [1, 2, 3]
    # [[LH-520]] Its first commit landed where DeclareTable placed it: the root, under the uuid the catalog minted.
    with httpx.Client(base_url=off_cluster_catalog_url, timeout=60) as catalog:
        described = catalog.post(f"/v1/table/{namespace}$sp%20ace/describe", json={"vend_credentials": False}).json()
    assert described["location"] == f"s3://{store.bucket}/{described['properties']['rask.uuid']}", described


#: A key the store refuses, as an ambient key outside the governed estate is: a client holding it reads only on what is vended.
def _refused_key(store: _Store) -> dict[str, str]:
    return {"aws_endpoint": store.url, "aws_region": _REGION, "allow_http": "true", "aws_access_key_id": "AKIAUNKNOWN", "aws_secret_access_key": "unknown"}


def _rows(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(list(ids), pa.int64())})


def _created(catalog: httpx.Client, _two_stores: Any, namespace: str, name: str) -> None:  # noqa: ANN401 — conftest.TwoStores
    made = catalog.post(
        f"/v1/table/{quote(f'{namespace}${name}', safe='$')}/create",
        content=encode_arrow_stream(_rows(1, 2, 3)),
        headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE},
    )
    assert made.status_code == 200, made.text


def _registered(catalog: httpx.Client, two_stores: Any, namespace: str, name: str) -> None:  # noqa: ANN401 — conftest.TwoStores
    """Bytes a pipeline wrote outside the catalog's doors, in a directory named after the table, then registered under that spelling."""
    options = {"endpoint": two_stores.url, "allow_http": "true", "region": _REGION, **two_stores.estate.model_dump()}
    written = f"s3://{two_stores.estate_bucket}/{namespace}-registered/{quote(name)}"
    lance.write_dataset(_rows(1, 2, 3), written, storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True)
    made = catalog.post(f"/v1/table/{quote(f'{namespace}${name}', safe='$')}/register", json={"location": f"{namespace}-registered/{name}"})
    assert made.status_code == 200, made.text


def _keys_held(two_stores: Any, location: str) -> list[str]:  # noqa: ANN401 — conftest.TwoStores
    """Every key the store holds under the table at ``location``: the path pylance opens, its location decoded once."""
    bucket, _, prefix = decoded_path(location).partition("/")
    s3 = boto3.client("s3", endpoint_url=two_stores.url, region_name=_REGION, **two_stores.estate.model_dump())  # noqa: TID251 — a test reading moto
    return [str(item["Key"]) for item in s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/").get("Contents", [])]


def _permits(policy: dict[str, Any], action: str, bucket: str, key: str) -> bool:
    """IAM's verdict on ``action`` over ``bucket/key``, by moto's evaluator.

    moto turns a Resource into a regex without escaping it, so a ``$`` in a key would read as an end anchor; it is swapped
    for a character IAM gives no meaning, on both sides alike.
    """
    document = json.dumps(policy, ensure_ascii=False).replace("$", "~")
    return IAMPolicy(document).is_action_permitted(action, f"arn:aws:s3:::{bucket}/{key.replace('$', '~')}") == PermissionResult.PERMITTED


@pytest.mark.parametrize(
    ("make", "name"),
    [
        pytest.param(_created, "räksmörgås", id="created-with-a-non-ascii-name"),
        pytest.param(_created, "growth%", id="created-with-a-percent-name"),
        pytest.param(_registered, "räksmörgås", id="registered-at-a-non-ascii-location"),
    ],
)
def test_every_vend_reaches_the_keys_the_store_keeps_for_the_table(
    two_stores: Any,  # noqa: ANN401 — conftest.TwoStores
    store: _Store,
    catalog_url: str,
    make: Callable[[httpx.Client, Any, str, str], None],
    name: str,
) -> None:
    """[[LH-520]] A stock client reads and writes a table on the vended credential, whatever its name or its registrar's spelling.

    The catalog answers a location holding a non-ASCII character or a ``%`` percent-encoded (``r%C3%A4ksm%C3%B6rg%C3%A5s``,
    ``growth%25``), and pylance opens the key it decodes to, so a session policy naming the answer's spelling reaches none of
    the table's bytes: measured as 403 AccessDenied on helm rev 20 ([[LH-517]]). A table the catalog makes lives under its
    uuid, which no name reaches (ADR 0173 §5); a registered table keeps its registrar's spelling, and its vend is scoped to
    the key that spelling decodes to.

    moto does not enforce a session policy, so the client's read and write go through either way: the policy each vend sent
    with its exchange is judged by moto's IAM evaluator against the keys the store holds for the table. That RustFS enforces
    it is the live read-back's claim.
    """
    namespace = f"db{uuid.uuid4().hex[:8]}"
    path = quote(f"{namespace}${name}", safe="$")
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        make(catalog, two_stores, namespace, name)
        location = str(catalog.post(f"/v1/table/{path}/describe", json={"vend_credentials": False}).json()["location"])
    issued = len(sts_backends[_ACCOUNT]["aws"].assumed_roles)

    stock = lance_namespace.connect("rest", {"uri": catalog_url})
    lance.write_dataset(_rows(4), namespace_client=stock, table_id=[namespace, name], mode="append", storage_options=_refused_key(store))
    read = lance.dataset(namespace_client=stock, table_id=[namespace, name], storage_options=_refused_key(store)).to_table().column("id").to_pylist()

    assert sorted(read) == [1, 2, 3, 4]
    vended = [json.loads(role.policy) for role in sts_backends[_ACCOUNT]["aws"].assumed_roles[issued:] if role.policy and role.session_name != "catalog"]
    assert vended, "the stock client was vended no credential"
    keys = _keys_held(two_stores, location)
    assert keys, f"precondition: the store holds the table's bytes under {decoded_path(location)}"
    unreadable = sorted({key for key in keys for policy in vended if not _permits(policy, "s3:GetObject", store.bucket, key)})
    unwritable = [key for key in keys if "/data/" in key and not any(_permits(policy, "s3:PutObject", store.bucket, key) for policy in vended)]
    assert (unreadable, unwritable) == ([], []), f"a vend for {location} is scoped to another prefix: {vended}"
