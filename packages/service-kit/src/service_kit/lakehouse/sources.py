"""Provider-agnostic external SOURCE adapters — the ingest seam.

A *source* is anything that yields raw objects to land in the lakehouse: a local directory, an S3/MinIO
bucket, or — as plugins outside this module — IIIF / GCS / HuggingFace / HCP. The lakehouse ingest needs
only each object's bytes plus a stable source URI (for lineage provenance); the provider client stays
behind the small ``SourceAdapter`` protocol so no provider code leaks into the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pyarrow.fs as pafs
from pydantic import BaseModel


class SourceObject(BaseModel):
    """One raw object to ingest: its bytes plus the stable ``uri`` recorded as lineage provenance."""

    model_config = {"frozen": True}  # an immutable value object crossing the adapter -> ingest boundary

    uri: str
    data: bytes


class SourceAdapter(Protocol):
    """Yields the objects to ingest. Real providers (IIIF/GCS/HF/HCP) implement this outside the lakehouse."""

    def iter_objects(self) -> Iterator[SourceObject]: ...


class KeyedSourceAdapter(SourceAdapter, Protocol):
    """A :class:`SourceAdapter` that can enumerate WITHOUT fetching.

    Optional, and it exists for a measured cost rather than for tidiness. A queued ingest plane
    enumerates first — to slice the work into chunks — and fetches later, in workers. With
    ``iter_objects`` as the only entry point, enumeration reads every object's bytes purely to obtain
    its ``uri`` and then discards them, so an ``n``-object source is transferred TWICE. Against a
    rate-limited IIIF volume the enumeration step alone is a full download of the volume, doubling
    the request load on exactly the endpoint the queue's backpressure exists to protect.

    Adapters that can list cheaply implement this; :func:`ingest.sources.iter_unit_keys` falls back
    to ``iter_objects`` for those that genuinely cannot, so the protocol stays optional.
    """

    def iter_keys(self) -> Iterator[str]: ...


class LocalDirSource:
    """A :class:`SourceAdapter` over a local directory tree — each file's bytes + its ``file://`` URI.

    Recurses (``rglob``) and yields in sorted path order, matching :class:`S3Source` so the two adapters
    are interchangeable behind the Protocol and both produce a deterministic, reproducible ingest order.
    """

    def __init__(self, root: Path, pattern: str = "*") -> None:
        self._root = root
        self._pattern = pattern

    def iter_keys(self) -> Iterator[str]:
        """The URIs alone — same objects, same order, no bytes read."""
        for path in sorted(self._root.rglob(self._pattern)):
            if path.is_file():
                yield path.resolve().as_uri()

    def iter_objects(self) -> Iterator[SourceObject]:
        for path in sorted(self._root.rglob(self._pattern)):
            if path.is_file():
                yield SourceObject(uri=path.resolve().as_uri(), data=path.read_bytes())


class S3Source:
    """A :class:`SourceAdapter` over an S3/MinIO bucket prefix — each object's bytes + its ``s3://`` URI.

    ``fs`` is a configured ``pyarrow.fs.S3FileSystem`` (endpoint + creds live there, not here), so the same
    adapter serves MinIO, RustFS, or AWS by swapping the filesystem — the exact provider-agnostic seam.
    """

    def __init__(self, fs: pafs.S3FileSystem, bucket: str, prefix: str = "") -> None:
        self._fs = fs
        self._bucket = bucket
        self._prefix = prefix

    def _listing(self) -> list[pafs.FileInfo]:
        base = "/".join(part for part in (self._bucket, self._prefix) if part)
        listing = self._fs.get_file_info(pafs.FileSelector(base, recursive=True))
        # Sort by path: S3 listing order is unspecified, and the bronze `id` is positional, so a stable
        # order is what makes the ingest reproducible (same objects -> same ids -> same rows every run).
        # Directory entries are the common prefixes a recursive listing returns; they are not objects.
        return [info for info in sorted(listing, key=lambda entry: entry.path) if info.type == pafs.FileType.File]

    def iter_keys(self) -> Iterator[str]:
        """The URIs alone — one LIST call, no object bodies transferred."""
        for info in self._listing():
            yield f"s3://{info.path}"

    def iter_objects(self) -> Iterator[SourceObject]:
        base = "/".join(part for part in (self._bucket, self._prefix) if part)
        listing = self._fs.get_file_info(pafs.FileSelector(base, recursive=True))
        # Sort by path: S3 listing order is unspecified, and the bronze `id` is positional, so a stable order
        # is what makes the ingest reproducible (same objects -> same ids -> same rows every run).
        for info in sorted(listing, key=lambda entry: entry.path):
            if info.type != pafs.FileType.File:
                continue  # skip the common-prefix "directory" entries a recursive listing returns
            try:
                with self._fs.open_input_stream(info.path) as stream:
                    data = stream.readall()
            except OSError as exc:  # name the object so a single unreadable key is diagnosable
                raise RuntimeError(f"failed to read source object {info.path!r}") from exc
            yield SourceObject(uri=f"s3://{info.path}", data=data)
