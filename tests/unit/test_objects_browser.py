"""The viewer's S3 object browser endpoints (ported from the retired volumes-api, R6/R20).

Driven over HTTP at the boundary: a minimal app with the objects router + the
problem+json handlers, S3 doubled by moto. Pins the wire shapes the lakehouse
zone's storage.ts mirrors (S3Listing / S3ObjectHead), the 404 mapping, and the
bucket allowlist rejection.

The fixture SUPPLIES its own store registry through `RASK_STORES` rather than
leaning on a platform default. That is the contract as of 2026-08-17: a
workload's buckets are deployment configuration, and `service-kit` no longer
ships any — a platform library asserting that an estate holds page images was
the platform having an opinion about the workload.

The registry declares two buckets and the fixture creates only the FIRST, so the
second is genuinely absent — which reproduces live-proof defect 2 (a Tenant that
never provisioned `spec.buckets`) exactly, as before.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from viewer.api.v1.endpoints import objects as objects_module
from viewer.api.v1.endpoints.objects import router as objects_router

from service_kit.exceptions import register_handlers


#: The DEPLOYMENT's store registry for this suite. Declared here because `service-kit` ships no
#: workload buckets any more: a test that needs a bucket supplies it, exactly as a deployment does.
FIXTURE_STORES = '[{"name": "corpus-raw", "bucket": "corpus-raw", "role": "raw", "description": "Fixture source bucket."}, {"name": "corpus-derived", "bucket": "corpus-derived", "role": "derived", "description": "Fixture bucket deliberately never provisioned."}]'


def _use_fixture_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the store registry at this suite's buckets. Needed by every test that builds its own
    client instead of taking the `client` fixture."""
    monkeypatch.setenv("RASK_STORES", FIXTURE_STORES)


def _app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(objects_router)
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Force every S3-endpoint alias empty in the env (which overrides the repo
    # `.env`) so `s3_client` falls back to no endpoint and boto3 — and thus moto —
    # uses its default AWS endpoint instead of a real one.
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("HCP_ENDPOINT", "")
    _use_fixture_stores(monkeypatch)

    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="corpus-raw")
        s3.put_object(Bucket="corpus-raw", Key="VOL/a.jpg", Body=b"hello", ContentType="image/jpeg")
        s3.put_object(Bucket="corpus-raw", Key="VOL/sub/b.jpg", Body=b"y")
        s3.put_object(Bucket="corpus-raw", Key="top.jpg", Body=b"z")

        with TestClient(_app()) as c:
            yield c


def test_list_objects_returns_prefixes_and_objects(client: TestClient) -> None:
    # Root level: one common-prefix folder ("VOL/") and one leaf object ("top.jpg").
    root = client.get("/api/objects", params={"bucket": "corpus-raw", "prefix": ""}).json()
    assert root["bucket"] == "corpus-raw"
    assert root["prefix"] == ""
    assert root["prefixes"] == ["VOL/"]
    assert [o["key"] for o in root["objects"]] == ["top.jpg"]
    assert root["objects"][0]["size"] == 1
    assert root["objects"][0]["last_modified"] is not None

    # Under "VOL/": the leaf "VOL/a.jpg" plus the sub-folder "VOL/sub/".
    vol = client.get("/api/objects", params={"bucket": "corpus-raw", "prefix": "VOL/"}).json()
    assert vol["prefixes"] == ["VOL/sub/"]
    assert [o["key"] for o in vol["objects"]] == ["VOL/a.jpg"]


def test_head_object_returns_metadata(client: TestClient) -> None:
    resp = client.get("/api/object", params={"bucket": "corpus-raw", "key": "VOL/a.jpg"})
    assert resp.status_code == 200
    head = resp.json()
    assert head["key"] == "VOL/a.jpg"
    assert head["size"] == 5
    assert head["content_type"] == "image/jpeg"
    assert head["last_modified"] is not None
    assert head["etag"]  # moto returns a (quoted) etag; the endpoint strips the quotes
    assert '"' not in head["etag"]


def test_head_missing_object_is_problem_404(client: TestClient) -> None:
    resp = client.get("/api/object", params={"bucket": "corpus-raw", "key": "VOL/missing.jpg"})
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["status"] == 404


def test_download_returns_bytes_with_disposition(client: TestClient) -> None:
    resp = client.get("/api/object/download", params={"bucket": "corpus-raw", "key": "VOL/a.jpg"})
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["content-type"].startswith("image/jpeg")
    # VS-10: the key is caller-supplied, so the disposition carries a sanitized ASCII fallback
    # plus the RFC 6266 pct-encoded form (which conforming clients prefer).
    assert resp.headers["content-disposition"] == "attachment; filename=\"a.jpg\"; filename*=UTF-8''a.jpg"


def test_download_missing_object_is_404(client: TestClient) -> None:
    resp = client.get("/api/object/download", params={"bucket": "corpus-raw", "key": "nope.bin"})
    assert resp.status_code == 404


def test_unlisted_bucket_is_rejected(client: TestClient) -> None:
    # R28: the allowlist is the catalog's STORAGE REGISTRY, not a Literal union — but the guarantee
    # is unchanged, and it is the one that matters: a bucket nobody registered is never listed.
    #
    # The status moved 422 -> 404 deliberately. An unregistered store is a MISSING RESOURCE, not a
    # malformed request: the browser populates its picker from the registry, so if it asks for a
    # store, the registry said that store existed. 404 tells it the registry changed underneath it;
    # 422 would claim the client sent nonsense it could not have constructed.
    resp = client.get("/api/objects", params={"bucket": "secrets", "prefix": ""})
    assert resp.status_code == 404
    assert "secrets" in resp.text


# --------------------------------------------------------------------------------------------------
# live-proof 2026-07-28, defect 2 — a bucket that does not exist is a DIAGNOSABLE 404, never a 500.
#
# `GET /api/objects?bucket=images-batch` raised an unhandled botocore NoSuchBucket (the listing route
# had no try/except at all), FastAPI turned it into a 500 with a traceback, and the lakehouse storage
# browser rendered "Storage service unreachable (HTTP 500)" — a message that is wrong about the layer,
# wrong about the cause, and names neither the bucket nor the fix. The bucket was absent because the
# RustFS Tenant had refused to reconcile; three layers up, nothing said so.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/objects", {"bucket": "corpus-derived", "prefix": ""}),
        ("/api/object", {"bucket": "corpus-derived", "key": "VOL/a.xml"}),
        ("/api/object/download", {"bucket": "corpus-derived", "key": "VOL/a.xml"}),
    ],
)
def test_a_missing_bucket_is_a_404_naming_the_bucket(client: TestClient, path: str, params: dict[str, str]) -> None:
    """All THREE routes, not just the listing that burned us — the siblings caught
    `Exception` and answered "object not found", which hides a missing bucket just as
    thoroughly as a traceback does."""
    resp = client.get(path, params=params)
    assert resp.status_code == 404, f"{path} must degrade to 404 on a missing bucket, got {resp.status_code}"
    assert resp.headers["content-type"] == "application/problem+json"
    detail = resp.json()["detail"]
    assert "corpus-derived" in detail, f"the answer must NAME the missing bucket: {detail!r}"
    assert "bucket not found" in detail, f"the answer must say it is the BUCKET that is missing: {detail!r}"
    assert "rustfs.buckets" in detail, f"the answer must point at the fix: {detail!r}"


def test_a_missing_key_and_a_missing_bucket_do_not_read_the_same(client: TestClient) -> None:
    """The two 404s must be distinguishable — otherwise an unprovisioned store is
    indistinguishable from an empty one, which is the whole failure mode."""
    missing_key = client.get("/api/object", params={"bucket": "corpus-raw", "key": "nope.jpg"}).json()["detail"]
    missing_bucket = client.get("/api/object", params={"bucket": "corpus-derived", "key": "nope.jpg"}).json()["detail"]
    assert missing_key.startswith("object not found")
    assert missing_bucket.startswith("bucket not found")


def test_a_body_less_404_still_names_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend-independence leg. A `HEAD` carries no body, so AWS answers a missing
    bucket with a bare `404` that looks exactly like a missing key (moto/MinIO volunteer
    `NoSuchBucket`; AWS does not). The endpoint must not let the quality of its own error
    message depend on which S3 implementation is plugged in — the storage-agnostic rule —
    so it probes the bucket on the error path. This stubs the AWS shape."""
    from botocore.exceptions import ClientError

    class _AwsShapedClient:
        def head_object(self, **_: object) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

        def head_bucket(self, **_: object) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")

    # Takes the endpoint arg the real `s3_client(endpoint=None)` has always accepted. Routes now pass
    # the STORE's endpoint (raw lives off-warehouse), so a zero-arg stub is narrower than the function
    # it stands in for — and a stub narrower than its subject fails on a change the subject allows.
    _use_fixture_stores(monkeypatch)
    monkeypatch.setattr(objects_module, "s3_client", lambda _endpoint=None, **_kw: _AwsShapedClient())
    with TestClient(_app()) as c:
        resp = c.get("/api/object", params={"bucket": "corpus-derived", "key": "k"})
    assert resp.status_code == 404
    assert "bucket not found: corpus-derived" in resp.json()["detail"]


def test_a_real_outage_is_not_disguised_as_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction of the same lie. `except Exception -> NotFoundError` reported an
    unreachable endpoint as "object not found"; a 404 for an outage sends the operator hunting
    for a key that was never the problem. Only S3's own not-found codes may become 404s."""

    class _DeadClient:
        def head_object(self, **_: object) -> dict[str, Any]:
            raise ConnectionError("Could not connect to the endpoint URL")

        def get_object(self, **_: object) -> dict[str, Any]:
            raise ConnectionError("Could not connect to the endpoint URL")

        def get_paginator(self, _name: str) -> Any:  # noqa: ANN401 — boto3 paginator has no public stub
            raise ConnectionError("Could not connect to the endpoint URL")

    _use_fixture_stores(monkeypatch)
    monkeypatch.setattr(objects_module, "s3_client", lambda _endpoint=None, **_kw: _DeadClient())
    with TestClient(_app(), raise_server_exceptions=False) as c:
        for path, params in (
            ("/api/objects", {"bucket": "corpus-raw", "prefix": ""}),
            ("/api/object", {"bucket": "corpus-raw", "key": "VOL/a.jpg"}),
            ("/api/object/download", {"bucket": "corpus-raw", "key": "VOL/a.jpg"}),
        ):
            assert c.get(path, params=params).status_code == 500, f"{path} must not report an outage as a 404"
