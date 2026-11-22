"""An ``s3://`` location split into its bucket and key, a key joined beneath one, and the prefix that lists it."""


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/optional/prefix/`` into ``(bucket, prefix)``, the prefix without a trailing slash."""
    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    return bucket, prefix.rstrip("/")


def s3_object(root_uri: str, key: str) -> tuple[str, str]:
    """The bucket and the full key of the object at ``key`` beneath the ``s3://`` location ``root_uri``."""
    bucket, prefix = split_s3_uri(root_uri)
    return bucket, f"{prefix}/{key}" if prefix else key


def s3_listing(root_uri: str) -> tuple[str, str]:
    """The bucket and the prefix that lists every object beneath the ``s3://`` location ``root_uri``: ``<key>/``, and the
    empty prefix, the whole bucket, for a location at its root."""
    bucket, prefix = split_s3_uri(root_uri)
    return bucket, f"{prefix}/" if prefix else ""


__all__ = [
    "s3_listing",
    "s3_object",
    "split_s3_uri",
]
