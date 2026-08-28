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

from service_kit.lancekit.arrow_ipc import encode_arrow_stream


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


class LanceFragmentSource:
    """A :class:`VersionedKeyedSourceAdapter` over an UNGOVERNED ``.lance`` dataset — one unit per FRAGMENT.

    The ingest-run form of "land an existing Lance table": ``POST /v1/table/{id}/register`` already makes
    an existing table governed in place, so this exists for the other case — reading an ungoverned dataset
    THROUGH the bronze plane, so the rows arrive with provenance, an anti-join and a cascade behind them.

    **The unit is a fragment, and that is a correctness choice, not a convenience.** The obvious grain is
    a row range, and the Lance docs refuse it: offsets "are not stable — a row with an offset of N may
    have a different offset in a different version of the table (e.g. if an earlier row is deleted)"
    (``lance_sdk.md``). This plane folds a unit key into the bronze row identity precisely so a re-run
    CONVERGES, so offset keys would re-land a table's whole tail after any early delete — the duplication
    the anti-join exists to prevent. Fragment ids are the stable alternative the format guarantees:
    ``row_address = (fragment_id << 32) | local_row_offset`` and ``ReserveFragments`` "only changes the
    max fragment id" (``file_format.md``), so ids are reserved and monotonic and never renumbered.

    The payload is the fragment's rows as an **Arrow IPC stream** — opaque bytes in bronze's blob column.
    That is what keeps the platform ignorant of the source's schema: any table, any modality, no column
    name known here. The reader is whatever consumes bronze, exactly as for a TIFF or a WAV.

    ``lance`` is imported lazily on purpose: ``service-kit`` is the dependency-light platform library, and
    a module-level import would put pylance in every service that imports anything from it.
    """

    def __init__(self, uri: str, storage_options: dict[str, str] | None = None) -> None:
        self._uri = uri
        self._storage_options = storage_options

    def _dataset(self) -> Any:  # noqa: ANN401 — lance.LanceDataset, unimportable at module scope by design
        import lance

        return lance.dataset(self._uri, storage_options=self._storage_options)

    def _key(self, fragment_id: int) -> str:
        """``<uri>#fragment=<id>`` — the dataset names the provenance, the fragment names the unit."""
        return f"{self._uri}#fragment={fragment_id}"

    def probe(self) -> int:
        """Open the dataset and count its fragments; raises if it cannot be read.

        The ACCEPT-time half of the plan's second guard. A source that cannot be opened must be refused
        while the caller is still holding the request, not discovered by a worker that has already
        claimed a unit and then has nothing to fetch.
        """
        return len(self._dataset().get_fragments())

    def iter_keys(self) -> Iterator[str]:
        """One key per fragment, in the dataset's own fragment order — no data read."""
        for fragment in self._dataset().get_fragments():
            yield self._key(fragment.fragment_id)

    def iter_versioned_keys(self) -> Iterator[tuple[str, str | None]]:
        """``(key, dataset_version)``.

        A fragment's id is stable but its CONTENT is not — a delete rewrites its deletion vector and
        leaves the id alone — so the key cannot identify what was ingested. The dataset version can, and
        it is free: one read of the manifest already open.
        """
        dataset = self._dataset()
        token = str(dataset.version)
        for fragment in dataset.get_fragments():
            yield self._key(fragment.fragment_id), token

    def iter_objects(self) -> Iterator[SourceObject]:
        for fragment in self._dataset().get_fragments():
            table = fragment.to_table()
            yield SourceObject(uri=self._key(fragment.fragment_id), data=encode_arrow_stream(table))
