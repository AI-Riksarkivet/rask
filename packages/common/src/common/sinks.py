"""Provider-agnostic external SINK adapters — the egress seam.

The mirror of :mod:`common.sources`: push curated artifacts OUT of the lakehouse to a local directory, an
S3/MinIO bucket, or — as plugins — any external store. The pipeline needs only ``put(key, data) -> uri``
(the returned URI is recorded as the terminal lineage output); the provider client stays behind the
``SinkAdapter`` protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pyarrow.fs as pafs


class SinkAdapter(Protocol):
    """Writes one object to an external sink and returns its stable URI (for lineage)."""

    def put(self, key: str, data: bytes) -> str: ...


class LocalDirSink:
    """A :class:`SinkAdapter` writing under a local directory; returns the ``file://`` URI."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, key: str, data: bytes) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()


class S3Sink:
    """A :class:`SinkAdapter` writing into an S3/MinIO bucket prefix; returns the ``s3://`` URI.

    ``fs`` is a configured ``pyarrow.fs.S3FileSystem`` (endpoint + creds live there), so the same adapter
    serves MinIO, RustFS, or AWS by swapping the filesystem.
    """

    def __init__(self, fs: pafs.S3FileSystem, bucket: str, prefix: str = "") -> None:
        self._fs = fs
        self._bucket = bucket
        self._prefix = prefix

    def put(self, key: str, data: bytes) -> str:
        # Explicit segment join (not a global "//"->"/" replace, which would also collapse a legitimate `//`
        # inside a key); dropping empty parts handles an unset prefix without corrupting the rest of the path.
        path = "/".join(part for part in (self._bucket, self._prefix, key) if part)
        try:
            with self._fs.open_output_stream(path) as stream:
                stream.write(data)
        except OSError as exc:
            raise RuntimeError(f"failed to write sink object {path!r}") from exc
        return f"s3://{path}"
