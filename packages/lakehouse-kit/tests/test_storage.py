def test_split_s3_uri_splits_bucket_from_prefix() -> None:
    from lakehouse_kit.storage import split_s3_uri

    assert split_s3_uri("s3://bucket/foo/bar/") == ("bucket", "foo/bar/")
    assert split_s3_uri("s3://bucket") == ("bucket", "")
