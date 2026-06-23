"""volumes-api endpoint smoke tests — FS-backed, no DB/Lance/Ray."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from volumes_api import app

    with TestClient(app) as c:
        yield c


def test_list_pages_empty_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_image_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/VOL/missing.jpg/image")
    assert resp.status_code == 404


def test_image_path_outside_volume_returns_400(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/OTHER/x.jpg/image")
    assert resp.status_code == 400


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_objects_returns_prefixes_and_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3
    from moto import mock_aws

    from service_kit.config import Settings
    from volumes_api import service as volumes_service

    # No HCP endpoint → boto3 (and thus moto) uses its default AWS endpoint.
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="images-batch")
        client.put_object(Bucket="images-batch", Key="VOL/a.jpg", Body=b"x")
        client.put_object(Bucket="images-batch", Key="VOL/sub/b.jpg", Body=b"y")
        client.put_object(Bucket="images-batch", Key="top.jpg", Body=b"z")

        # Pass HCP_ENDPOINT="" so the repo `.env`'s real endpoint can't leak in;
        # `s3_client` treats the empty string as falsy and falls back to no
        # endpoint, so boto3 (and thus moto) uses its default AWS endpoint.
        settings = Settings(
            RASK_VIEWER_INPUT="s3://images-batch",
            RASK_VIEWER_OUTPUT="s3://images-batch-alto",
            HCP_ENDPOINT="",
        )

        # Root level: one common-prefix folder ("VOL/") and one leaf object ("top.jpg").
        root = volumes_service.list_objects(settings, "images-batch", "")
        assert root.bucket == "images-batch"
        assert root.prefix == ""
        assert root.prefixes == ["VOL/"]
        assert [o.key for o in root.objects] == ["top.jpg"]
        assert root.objects[0].size == 1
        assert root.objects[0].last_modified is not None

        # Under "VOL/": the leaf "VOL/a.jpg" plus the sub-folder "VOL/sub/".
        vol = volumes_service.list_objects(settings, "images-batch", "VOL/")
        assert vol.prefixes == ["VOL/sub/"]
        assert [o.key for o in vol.objects] == ["VOL/a.jpg"]
