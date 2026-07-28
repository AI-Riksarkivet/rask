"""The viewer's S3 object browser endpoints (ported from the retired volumes-api, R6/R20).

Driven over HTTP at the boundary: a minimal app with the objects router + the
problem+json handlers, S3 doubled by moto. Pins the wire shapes the lakehouse
zone's storage.ts mirrors (S3Listing / S3ObjectHead), the 404 mapping, and the
bucket allowlist rejection.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from viewer.api.v1.endpoints.objects import router as objects_router

from service_kit.media.handlers import register_handlers


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Force every S3-endpoint alias empty in the env (which overrides the repo
    # `.env`) so `s3_client` falls back to no endpoint and boto3 — and thus moto —
    # uses its default AWS endpoint instead of a real one.
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("HCP_ENDPOINT", "")

    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="images-batch")
        s3.put_object(Bucket="images-batch", Key="VOL/a.jpg", Body=b"hello", ContentType="image/jpeg")
        s3.put_object(Bucket="images-batch", Key="VOL/sub/b.jpg", Body=b"y")
        s3.put_object(Bucket="images-batch", Key="top.jpg", Body=b"z")

        app = FastAPI()
        register_handlers(app)
        app.include_router(objects_router)
        with TestClient(app) as c:
            yield c


def test_list_objects_returns_prefixes_and_objects(client: TestClient) -> None:
    # Root level: one common-prefix folder ("VOL/") and one leaf object ("top.jpg").
    root = client.get("/api/objects", params={"bucket": "images-batch", "prefix": ""}).json()
    assert root["bucket"] == "images-batch"
    assert root["prefix"] == ""
    assert root["prefixes"] == ["VOL/"]
    assert [o["key"] for o in root["objects"]] == ["top.jpg"]
    assert root["objects"][0]["size"] == 1
    assert root["objects"][0]["last_modified"] is not None

    # Under "VOL/": the leaf "VOL/a.jpg" plus the sub-folder "VOL/sub/".
    vol = client.get("/api/objects", params={"bucket": "images-batch", "prefix": "VOL/"}).json()
    assert vol["prefixes"] == ["VOL/sub/"]
    assert [o["key"] for o in vol["objects"]] == ["VOL/a.jpg"]


def test_head_object_returns_metadata(client: TestClient) -> None:
    resp = client.get("/api/object", params={"bucket": "images-batch", "key": "VOL/a.jpg"})
    assert resp.status_code == 200
    head = resp.json()
    assert head["key"] == "VOL/a.jpg"
    assert head["size"] == 5
    assert head["content_type"] == "image/jpeg"
    assert head["last_modified"] is not None
    assert head["etag"]  # moto returns a (quoted) etag; the endpoint strips the quotes
    assert '"' not in head["etag"]


def test_head_missing_object_is_problem_404(client: TestClient) -> None:
    resp = client.get("/api/object", params={"bucket": "images-batch", "key": "VOL/missing.jpg"})
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["status"] == 404


def test_download_returns_bytes_with_disposition(client: TestClient) -> None:
    resp = client.get("/api/object/download", params={"bucket": "images-batch", "key": "VOL/a.jpg"})
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.headers["content-disposition"] == 'attachment; filename="a.jpg"'


def test_download_missing_object_is_404(client: TestClient) -> None:
    resp = client.get("/api/object/download", params={"bucket": "images-batch", "key": "nope.bin"})
    assert resp.status_code == 404


def test_unlisted_bucket_is_rejected(client: TestClient) -> None:
    # The Literal allowlist: anything outside the two fixed rask buckets is a 422,
    # never a listing of an arbitrary bucket.
    resp = client.get("/api/objects", params={"bucket": "secrets", "prefix": ""})
    assert resp.status_code == 422
