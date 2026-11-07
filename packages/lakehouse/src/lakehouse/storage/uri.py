"""S3 URI parsing."""


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split `s3://bucket/optional/prefix/` into (bucket, prefix)."""
    rest = uri.removeprefix("s3://")
    bucket, _, uri_prefix = rest.partition("/")
    return bucket, uri_prefix
