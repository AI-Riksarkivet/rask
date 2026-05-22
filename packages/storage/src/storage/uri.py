"""URI parsing + Source/Sink factories from string URIs."""

import functools
from pathlib import Path
from typing import Any

from storage.client import s3_client
from storage.fs import FSSink, FSSource
from storage.s3 import S3Sink, S3Source


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split `s3://bucket/optional/prefix/` into (bucket, prefix)."""
    rest = uri.removeprefix("s3://")
    bucket, _, uri_prefix = rest.partition("/")
    return bucket, uri_prefix


def merge_prefix(uri_prefix: str, flag_prefix: str) -> str:
    """Concatenate prefix from URI path and a flag (URI part comes first)."""
    parts = [p for p in (uri_prefix, flag_prefix) if p]
    if not parts:
        return ""
    merged = "/".join(p.strip("/") for p in parts)
    return merged + "/" if merged else ""


def build_source(
    uri: str,
    *,
    s3_endpoint: str | None = None,
    prefix: str = "",
    suffixes: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".tif", ".tiff"),
) -> Any:  # noqa: ANN401
    if uri.startswith("s3://"):
        bucket, uri_prefix = split_s3_uri(uri)
        full_prefix = merge_prefix(uri_prefix, prefix)
        factory = functools.partial(s3_client, s3_endpoint)
        return S3Source(bucket=bucket, prefix=full_prefix, suffixes=suffixes, client_factory=factory)
    root = Path(uri) / prefix if prefix else Path(uri)
    return FSSource(root=root, suffixes=suffixes)


def build_sink(uri: str, *, s3_endpoint: str | None = None, prefix: str = "") -> Any:  # noqa: ANN401
    if uri.startswith("s3://"):
        bucket, uri_prefix = split_s3_uri(uri)
        full_prefix = merge_prefix(uri_prefix, prefix)
        factory = functools.partial(s3_client, s3_endpoint)
        return S3Sink(bucket=bucket, prefix=full_prefix, client_factory=factory)
    root = Path(uri) / prefix if prefix else Path(uri)
    return FSSink(root=root)
