from pathlib import Path

from moto import mock_aws


def _make_s3_client():
    """Module-level factory for pickling tests."""
    import boto3

    return boto3.client("s3", region_name="us-east-1")


def test_fs_source_lists_jpg(tmp_path: Path):
    from storage import FSSource

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"y")
    src = FSSource(root=tmp_path)
    keys = sorted(src.keys())
    assert keys == ["a.jpg"]
    assert src.read("a.jpg") == b"x"


def test_fs_sink_writes(tmp_path: Path):
    from storage import FSSink

    sink = FSSink(root=tmp_path)
    sink.write("foo/bar.xml", b"<alto/>")
    assert (tmp_path / "foo" / "bar.xml").read_bytes() == b"<alto/>"
    assert "foo/bar.xml" in list(sink.existing_keys())


def test_s3_source_round_trip():
    import boto3

    from storage import S3Source

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="bucket-in")
        c.put_object(Bucket="bucket-in", Key="A0060198/00001.jpg", Body=b"img")

        src = S3Source(bucket="bucket-in", prefix="A0060198/", client=c)
        assert list(src.keys()) == ["A0060198/00001.jpg"]
        assert src.read("A0060198/00001.jpg") == b"img"


def test_factory_pickles():
    """S3Source/S3Sink built with a client_factory must drop the live client on pickle."""
    import pickle

    from storage import S3Source

    src = S3Source(bucket="b", prefix="p/", client_factory=_make_s3_client)
    _ = src.client  # forces lazy build
    blob = pickle.dumps(src)
    restored = pickle.loads(blob)  # noqa: S301
    assert restored._client is None  # rebuilt on next access via factory


def test_split_s3_uri_and_merge_prefix():
    from storage import merge_prefix, split_s3_uri

    assert split_s3_uri("s3://bucket/foo/bar/") == ("bucket", "foo/bar/")
    assert split_s3_uri("s3://bucket") == ("bucket", "")
    assert merge_prefix("foo/", "bar/") == "foo/bar/"
    assert merge_prefix("", "bar/") == "bar/"
    assert merge_prefix("", "") == ""


def test_derive_hcp_creds(monkeypatch):
    import os

    from storage import derive_hcp_creds

    monkeypatch.setenv("HCP_USERNAME", "alice")
    monkeypatch.setenv("HCP_PASSWORD", "secret")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    derive_hcp_creds()
    assert os.environ["AWS_ACCESS_KEY_ID"] == "YWxpY2U="
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "5ebe2294ecd0e0f08eab7690d2a6ee69"  # noqa: S105
