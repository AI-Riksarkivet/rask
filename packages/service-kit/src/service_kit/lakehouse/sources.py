"""Provider-agnostic external SOURCE adapters — the ingest seam.

A *source* is anything that yields raw objects to land in the lakehouse: a local directory, an S3/MinIO
bucket, or — as plugins outside this module — any protocol a workload needs. The lakehouse ingest needs
only each object's bytes plus a stable source URI (for lineage provenance); the provider client stays
behind the small ``SourceAdapter`` protocol so no provider code leaks into the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

import pyarrow.fs as pafs
from pydantic import BaseModel


class SourceObject(BaseModel):
    """One raw object to ingest: its bytes plus the stable ``uri`` recorded as lineage provenance."""

    model_config = {"frozen": True}  # an immutable value object crossing the adapter -> ingest boundary

    uri: str
    data: bytes


class SourceAdapter(Protocol):
    """Yields the objects to ingest. Real providers (GCS/HF/HCP/a workload's own protocol) implement this outside the lakehouse."""

    def iter_objects(self) -> Iterator[SourceObject]: ...


class KeyedSourceAdapter(SourceAdapter, Protocol):
    """A :class:`SourceAdapter` that can enumerate WITHOUT fetching.

    Optional, and it exists for a measured cost rather than for tidiness. A queued ingest plane
    enumerates first — to slice the work into chunks — and fetches later, in workers. With
    ``iter_objects`` as the only entry point, enumeration reads every object's bytes purely to obtain
    its ``uri`` and then discards them, so an ``n``-object source is transferred TWICE. Against a
    rate-limited HTTP source the enumeration step alone can be a full download of the unit, doubling
    the request load on exactly the endpoint the queue's backpressure exists to protect.

    Adapters that can list cheaply implement this; :func:`ingest.sources.iter_unit_keys` falls back
    to ``iter_objects`` for those that genuinely cannot, so the protocol stays optional.
    """

    def iter_keys(self) -> Iterator[str]: ...


class VersionedKeyedSourceAdapter(KeyedSourceAdapter, Protocol):
    """A :class:`KeyedSourceAdapter` whose listing carries a per-object VERSION TOKEN.

    The owner ruled (2026-08-07) that sinks DO replace objects under the same key, so a key alone
    cannot identify what was ingested: the bronze row identity is ``sha256(key + token)`` and a
    replaced object lands as a NEW row while the old one stays (bronze is history). The token is
    whatever the store hands out FREE at listing time — for S3 the ``ETag`` from ``list_objects_v2``,
    zero extra calls.

    Optional, exactly like :class:`KeyedSourceAdapter`: a source with no version story degrades to
    ``(key, None)`` — SNAPSHOT semantics, where a re-harvest is an explicit operator decision. That
    degradation is a documented contract, not an accident: only the S3 kind promises
    replace-in-place detection.
    """

    def iter_versioned_keys(self) -> Iterator[tuple[str, str | None]]: ...


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

    def __init__(self, fs: pafs.S3FileSystem, bucket: str, prefix: str = "", client: Any | None = None) -> None:  # noqa: ANN401 — boto3 has no public stubs (same rule as storage.client)
        self._fs = fs
        self._bucket = bucket
        self._prefix = prefix
        #: A boto3-compatible client (``storage.s3_client``) for the VERSIONED listing. Optional and
        #: additive: pyarrow FileInfo carries no ETag, so version tokens need list_objects_v2 — and
        #: the estate rule is storage.s3_client, never raw boto3 in service code. Absent -> the
        #: adapter still works, degraded to token-less snapshot listing.
        self._client = client

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

    def iter_versioned_keys(self) -> Iterator[tuple[str, str | None]]:
        """``(uri, etag)`` pairs from ONE paginated ``list_objects_v2`` — tokens cost nothing extra.

        S3 guarantees keys are returned in UTF-8 binary order, so this listing is deterministic
        without a sort — the same reproducibility contract ``_listing`` enforces by sorting. The
        ETag is stripped of the quotes S3 wraps it in. Without a client this degrades to
        ``(key, None)`` over the pyarrow listing: same keys, snapshot semantics.
        """
        if self._client is None:
            for key in self.iter_keys():
                yield key, None
            return
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
            for obj in page.get("Contents", []):
                etag = str(obj.get("ETag") or "").strip('"') or None
                yield f"s3://{self._bucket}/{obj['Key']}", etag

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
