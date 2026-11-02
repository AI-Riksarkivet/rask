"""The runner's sources and sinks: filesystem and S3, answering in storage's error taxonomy, and picklable for Ray."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import TYPE_CHECKING, cast

import boto3
import pytest
from moto import mock_aws

from htr.sources import FSSink, FSSource, S3Sink, S3Source, Sink, Source, build_sink, build_source, merge_prefix
from storage import BucketNotFoundError, ObjectNotFoundError


if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def _client() -> S3Client:
    return boto3.client("s3", region_name="us-east-1")


def test_fs_source_lists_only_image_keys_and_reads_them(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"y")
    source = FSSource(root=tmp_path)

    assert sorted(source.keys()) == ["a.jpg"]
    assert source.read("a.jpg") == b"x"


def test_fs_sink_writes_under_nested_keys_and_lists_them(tmp_path: Path) -> None:
    sink = FSSink(root=tmp_path)
    sink.write("foo/bar.xml", b"<alto/>")

    assert (tmp_path / "foo" / "bar.xml").read_bytes() == b"<alto/>"
    assert list(sink.existing_keys()) == ["foo/bar.xml"]


@mock_aws
def test_s3_source_lists_under_its_prefix_and_reads() -> None:
    client = _client()
    client.create_bucket(Bucket="bucket-in")
    client.put_object(Bucket="bucket-in", Key="A0060198/00001.jpg", Body=b"img")
    client.put_object(Bucket="bucket-in", Key="A0060198/notes.txt", Body=b"txt")

    source = S3Source(bucket="bucket-in", prefix="A0060198/", client=client)

    assert list(source.keys()) == ["A0060198/00001.jpg"]
    assert source.read("A0060198/00001.jpg") == b"img"


@mock_aws
def test_reading_a_missing_key_raises_storages_own_error() -> None:
    client = _client()
    client.create_bucket(Bucket="real")
    with pytest.raises(ObjectNotFoundError) as caught:
        S3Source(bucket="real", client=client).read("nope.jpg")
    assert (caught.value.bucket, caught.value.key) == ("real", "nope.jpg")


@mock_aws
def test_listing_a_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        list(S3Source(bucket="ghost", client=_client()).keys())


@mock_aws
def test_writing_into_a_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        S3Sink(bucket="ghost", client=_client()).write("out/a.xml", b"<alto/>")


@mock_aws
def test_listing_a_sinks_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        list(S3Sink(bucket="ghost", client=_client()).existing_keys())


def test_a_credential_failure_is_not_laundered_into_a_not_found() -> None:
    """The taxonomy translates not-founds and nothing else: an outage deserves a 5xx, not a 404."""
    from botocore.exceptions import ClientError

    class _Denied:
        def get_object(self, **kwargs: object) -> dict:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")

    with pytest.raises(ClientError):
        S3Source(bucket="real", client=cast("S3Client", _Denied())).read("k")


def test_a_uri_builds_the_adapter_for_its_scheme(tmp_path: Path) -> None:
    source, sink = build_source(str(tmp_path)), build_sink(str(tmp_path / "out"))
    s3_source, s3_sink = build_source("s3://b/in/", prefix="p/"), build_sink("s3://b/out", prefix="q")

    assert (isinstance(source, FSSource), isinstance(sink, FSSink)) == (True, True)
    assert (isinstance(source, Source), isinstance(sink, Sink)) == (True, True)
    assert (type(s3_source), s3_source.bucket, s3_source.prefix) == (S3Source, "b", "in/p/")
    assert (type(s3_sink), s3_sink.bucket, s3_sink.prefix) == (S3Sink, "b", "out/q/")


def test_merge_prefix_joins_the_uri_part_before_the_flag_part() -> None:
    assert merge_prefix("foo/", "bar/") == "foo/bar/"
    assert merge_prefix("", "bar/") == "bar/"
    assert merge_prefix("", "") == ""


def test_an_adapter_needs_a_client_or_a_factory() -> None:
    with pytest.raises(ValueError, match="S3Source"):
        S3Source(bucket="b")
    with pytest.raises(ValueError, match="S3Sink"):
        S3Sink(bucket="b")


def test_a_factory_built_adapter_drops_its_client_on_pickle() -> None:
    for adapter in (S3Source(bucket="b", prefix="p/", client_factory=_client), S3Sink(bucket="b", client_factory=_client)):
        _ = adapter.client
        restored = pickle.loads(pickle.dumps(adapter))  # noqa: S301
        assert restored._client is None
        assert restored.bucket == "b"
