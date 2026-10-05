"""A warehouse naming another object store is refused, because the catalog would sign toward it with the estate's key ([[LH-205]]).

The catalog holds one credential pair, the estate's. A warehouse record may name an `endpoint`, but no
door consumes a per-warehouse credential, so every open under such a record signed toward the named
host with the estate's access-key id, its bucket was provisioned on the estate's store, and its vend
ran against the estate's STS. Two claims, one test each, both through the real app:

- the create door refuses any endpoint that is not the estate's (a respelling of the estate's endpoint
  is still the estate's, and a spelling that only looks like it is not);
- a record that already names another store, seeded straight into the control root because the create
  door now refuses it, is refused at every door that would reach a store for it.

The foreign host is a real HTTP listener that records every request it receives, so "nothing signed
toward it" is observed at the host rather than inferred. The estate is a moto S3 server, so a
provision or purge that reached the estate's store shows up in its bucket listing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catalog.services import projects, warehouses
from storage import s3_client


_ESTATE_KEY_ID = "estate-key-id"
_ESTATE_SECRET = "estate-secret"
_DENIED = b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code><Message>foreign store</Message></Error>'


class _ForeignStore(ThreadingHTTPServer):
    """An object store at a host the estate does not own: every request is recorded and refused."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Recorder)
        self.seen: list[str] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Recorder(BaseHTTPRequestHandler):
    """Records the method, path and the credential a request was signed with, then answers 403 so no client retries."""

    server: _ForeignStore

    def _refuse(self) -> None:
        signed_with = self.headers.get("Authorization", "").split(",")[0]
        self.server.seen.append(f"{self.command} {self.path} {signed_with}")
        self.send_response(403)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(_DENIED)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(_DENIED)

    do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _refuse

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's own parameter name
        return


@pytest.fixture
def foreign_store() -> Iterator[_ForeignStore]:
    server = _ForeignStore()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def catalog(tmp_path: Path, moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real catalog app with warehouses on, a local control root, and the moto server as the estate's store."""
    (tmp_path / "default").mkdir()
    (tmp_path / "registry").mkdir()
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path / "default"))
    monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
    monkeypatch.setenv("LANCE_CONTROL_ROOT", _control(tmp_path))
    monkeypatch.setenv("LANCE_S3_ENDPOINT", moto_url)
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", _ESTATE_KEY_ID)
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", _ESTATE_SECRET)

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _control(tmp_path: Path) -> str:
    return f"file://{tmp_path / 'registry'}"


def _seed_project(tmp_path: Path) -> None:
    projects.put_project(_control(tmp_path), {}, {"id": "acme", "created_at": "t", "created_by": "x", "protected": "false"})


def _estate_buckets(moto_url: str) -> set[str]:
    client = s3_client(moto_url, access_key=_ESTATE_KEY_ID, secret_key=_ESTATE_SECRET)
    return {bucket["Name"] for bucket in client.list_buckets().get("Buckets", [])}


@pytest.mark.parametrize(
    ("warehouse", "endpoint", "status", "code", "provisioned"),
    [
        pytest.param("crt-other", "{foreign}", 400, 13, False, id="another-store"),
        pytest.param("crt-userinfo", "http://{estate_host}@{foreign_host}", 400, 13, False, id="estate-as-userinfo"),
        pytest.param("crt-respelled", "{estate_upper}/", 200, None, True, id="estate-respelled"),
    ],
)
def test_create_refuses_an_endpoint_that_is_not_the_estates(
    catalog: TestClient,
    foreign_store: _ForeignStore,
    moto_url: str,
    tmp_path: Path,
    warehouse: str,
    endpoint: str,
    status: int,
    code: int | None,
    provisioned: bool,
) -> None:
    _seed_project(tmp_path)
    named = endpoint.format(
        foreign=foreign_store.url,
        estate_host=moto_url.removeprefix("http://"),
        foreign_host=foreign_store.url.removeprefix("http://"),
        estate_upper=moto_url.upper(),
    )
    before = _estate_buckets(moto_url)

    response = catalog.post("/v1/warehouses", json={"id": warehouse, "project": "acme", "endpoint": named})

    recorded = warehouses.get_warehouse(_control(tmp_path), {}, warehouse) is not None
    created = _estate_buckets(moto_url) - before
    outcome = (response.status_code, response.json().get("code"), recorded, created, foreign_store.seen)
    assert outcome == (status, code, provisioned, {warehouse} if provisioned else set(), []), response.text


@pytest.mark.parametrize(
    ("warehouse", "method", "path", "body"),
    [
        pytest.param("far-open", "POST", "/v1/table/fns$t/describe", {}, id="open"),
        pytest.param("far-vend", "POST", "/management/v1/table/fns$t/credentials", None, id="vend"),
        pytest.param("far-probe", "POST", "/v1/warehouses/{warehouse}/validate", None, id="vend-probe"),
        pytest.param("far-provision", "POST", "/v1/warehouses", {"id": "{warehouse}", "project": "acme"}, id="provision"),
        pytest.param("far-purge", "DELETE", "/v1/warehouses/{warehouse}?cascade=true&purge_bucket=true", None, id="purge"),
        pytest.param("far-cascade", "DELETE", "/v1/warehouses/{warehouse}?cascade=true", None, id="cascade"),
        pytest.param("far-bind", "POST", "/v1/warehouses/{warehouse}/namespaces", {"namespace": "fresh"}, id="create-namespace"),
        pytest.param("far-unbind", "DELETE", "/v1/warehouses/{warehouse}/namespaces/fns", None, id="unbind"),
        pytest.param("far-list", "GET", "/v1/namespace/fns/table/list", None, id="table-list"),
    ],
)
def test_a_record_naming_another_store_is_refused_at_every_door(
    catalog: TestClient,
    foreign_store: _ForeignStore,
    moto_url: str,
    tmp_path: Path,
    warehouse: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    control = _control(tmp_path)
    _seed_project(tmp_path)
    warehouses.put_warehouse(
        control,
        {},
        {
            "id": warehouse,
            "bucket": warehouse,
            "root_uri": f"s3://{warehouse}",
            "project": "acme",
            "status": "active",
            "created_at": "t",
            "endpoint": foreign_store.url,
        },
    )
    warehouses.bind_namespace(control, {}, "fns", warehouse, f"s3://{warehouse}")
    before = _estate_buckets(moto_url)

    response = catalog.request(
        method,
        path.format(warehouse=warehouse),
        json={key: value.format(warehouse=warehouse) for key, value in body.items()} if body is not None else None,
    )

    changed = _estate_buckets(moto_url) ^ before
    assert (response.status_code, response.json().get("code"), changed, foreign_store.seen) == (406, 0, set(), []), response.text
