"""Tests for storage.iiif: manifest parsing + IIIFCachedSource read-through."""

import pickle
import typing

import boto3
import httpx
import pytest
from moto import mock_aws


def _manifest_json(batch: str, ids: list[str]) -> dict:
    return {"items": [{"id": f"https://iiifintern-ai.ra.se/arkis!{img}/canvas"} for img in ids]}


def _httpx_mock(handler):
    """Build an httpx.Client backed by an httpx.MockTransport handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_image_ids_parses_manifest():
    from htr.iiif import get_image_ids

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/arkis!A0060198/manifest")
        return httpx.Response(200, json=_manifest_json("A0060198", ["A0060198_00002", "A0060198_00001"]))

    with _httpx_mock(handler) as client:
        ids = get_image_ids("A0060198", client=client)
    assert ids == ["A0060198_00001", "A0060198_00002"]  # sorted


def test_build_image_url_default():
    from htr.iiif import DEFAULT_IIIF_BASE, build_image_url

    url = build_image_url("A0060198_00001")
    assert url == f"{DEFAULT_IIIF_BASE}/arkis!A0060198_00001/full/max/0/default.jpg"


def test_build_image_url_custom_size():
    from htr.iiif import build_image_url

    url = build_image_url(
        "A0060198_00001",
        base_url="https://example.com",
        query_params="full/,1200/0/default.jpg",
    )
    assert url == "https://example.com/arkis!A0060198_00001/full/,1200/0/default.jpg"


def test_iiif_cached_source_hits_cache():
    """When the key already exists in S3, read() returns immediately without IIIF call."""
    from htr.iiif import IIIFCachedSource

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="cache")
        c.put_object(Bucket="cache", Key="A0060198/A0060198_00001.jpg", Body=b"cached-bytes")

        # If the source ever calls IIIF here, this handler raises.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"unexpected IIIF call: {request.url}")

        src = IIIFCachedSource(
            batch_ids=["A0060198"],
            cache_bucket="cache",
            s3_client_factory=lambda: c,
        )
        src._http = _httpx_mock(handler)  # bypass lazy property

        data = src.read("A0060198/A0060198_00001.jpg")
        assert data == b"cached-bytes"


def test_iiif_cached_source_misses_then_writes_through():
    """On cache miss, fetch from IIIF and write to S3."""
    from htr.iiif import IIIFCachedSource

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="cache")  # empty cache

        iiif_payload = b"\xff\xd8\xff\xe0iiif-bytes"

        def handler(request: httpx.Request) -> httpx.Response:
            # Expected URL for the cache miss
            assert "/arkis!A0060198_00001/" in str(request.url)
            return httpx.Response(200, content=iiif_payload)

        src = IIIFCachedSource(
            batch_ids=["A0060198"],
            cache_bucket="cache",
            s3_client_factory=lambda: c,
        )
        src._http = _httpx_mock(handler)

        data = src.read("A0060198/A0060198_00001.jpg")
        assert data == iiif_payload

        # Write-through happened
        cached = c.get_object(Bucket="cache", Key="A0060198/A0060198_00001.jpg")["Body"].read()
        assert cached == iiif_payload


def test_iiif_cached_source_keys_iterates_manifests():
    from htr.iiif import IIIFCachedSource

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="cache")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/arkis!A0060198/manifest"):
                return httpx.Response(200, json=_manifest_json("A0060198", ["A0060198_00001", "A0060198_00002"]))
            if path.endswith("/arkis!C0074667/manifest"):
                return httpx.Response(200, json=_manifest_json("C0074667", ["C0074667_00001"]))
            raise AssertionError(f"unexpected URL: {request.url}")

        src = IIIFCachedSource(
            batch_ids=["A0060198", "C0074667"],
            cache_bucket="cache",
            s3_client_factory=lambda: c,
        )
        src._http = _httpx_mock(handler)

        keys = list(src.keys())
    assert keys == [
        "A0060198/A0060198_00001.jpg",
        "A0060198/A0060198_00002.jpg",
        "C0074667/C0074667_00001.jpg",
    ]


def test_iiif_cached_source_pickles():
    """Live clients are dropped on pickle so the source can ship to Ray actors.

    Uses the production factory pattern (functools.partial wrapping a
    module-level function) — lambdas aren't picklable.
    """
    from htr.iiif import IIIFCachedSource

    src = IIIFCachedSource(
        batch_ids=["A0060198"],
        cache_bucket="cache",
        s3_endpoint="http://localhost:9000",
    )
    blob = pickle.dumps(src)
    restored = pickle.loads(blob)  # noqa: S301 — test-only round trip
    assert restored.batch_ids == ["A0060198"]
    assert restored.cache_bucket == "cache"
    assert restored._s3 is None  # cleared by __getstate__
    assert restored._http is None


def test_iiif_cached_source_propagates_non_404_s3_errors():
    """A connection-level S3 failure must not silently fall through to IIIF."""
    from htr.iiif import IIIFCachedSource

    class _AccessDenied(Exception):
        response: typing.ClassVar[dict] = {"Error": {"Code": "AccessDenied"}}

    class _BrokenS3:
        def get_object(self, **_):
            raise _AccessDenied()

    src = IIIFCachedSource(
        batch_ids=["A0060198"],
        cache_bucket="cache",
        s3_client_factory=lambda: _BrokenS3(),
    )

    with pytest.raises(_AccessDenied):
        src.read("A0060198/A0060198_00001.jpg")
