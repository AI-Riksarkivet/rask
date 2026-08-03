"""Source adapters, registered — the concrete half of I1.

Every adapter here already existed. `LocalDirSource` and `S3Source` have been in
`service_kit.lakehouse.sources` all along, and `S3PrefixSource` + its `s3_input()` lineage twin have
sat in `medallion/services/s3_harvest.py` unit-tested against moto with NO ROUTE WIRED — recorded as
open work for months. They were unreachable not because they were unfinished but because reaching
them meant adding another head route, another settings block, another produce module. That is the
cost I1 removes.

Adding a source is now: one adapter (often already written), one `register()` call, one lineage
twin. Gate A9 says a diff that does more than that has re-welded something.

Importing this module is what populates the registry, so `ingest/__init__.py` imports it for effect.
That is a deliberate import-time side effect: the alternative is a hand-maintained list somewhere
else, which is the drift this design exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ingest.sources import LineageInput, SourceSpec, register


if TYPE_CHECKING:
    from service_kit.lakehouse.sources import SourceAdapter


def _local_dir(spec: SourceSpec) -> SourceAdapter:
    """A directory tree. The dummy lane's fixture source — deterministic, no network (A11)."""
    from service_kit.lakehouse.sources import LocalDirSource

    root = str(spec.options.get("root") or "")
    if not root:
        raise ValueError("local-dir source requires options.root")
    return LocalDirSource(Path(root), str(spec.options.get("pattern") or "*"))


def _local_dir_lineage(spec: SourceSpec) -> LineageInput:
    return LineageInput(namespace="file", name=str(spec.options.get("root") or ""))


def _s3_prefix(spec: SourceSpec) -> SourceAdapter:
    """An S3 prefix over the estate's provider-agnostic client.

    `storage.s3_client` rather than boto3 directly — the estate's rule, and the reason a bucket can
    move between RustFS, MinIO, HCP and AWS with env vars instead of a code change.
    """
    from pyarrow import fs as pafs

    from service_kit.lakehouse.sources import S3Source

    bucket = str(spec.options.get("bucket") or "")
    if not bucket:
        raise ValueError("s3-prefix source requires options.bucket")
    endpoint = spec.options.get("endpoint")
    fs = pafs.S3FileSystem(endpoint_override=str(endpoint)) if endpoint else pafs.S3FileSystem()
    return S3Source(fs, bucket, str(spec.options.get("prefix") or ""))


def _s3_prefix_lineage(spec: SourceSpec) -> LineageInput:
    """The `s3://bucket` / prefix pair the lineage graph already MERGEs on."""
    return LineageInput(namespace=f"s3://{spec.options.get('bucket')}", name=str(spec.options.get("prefix") or "/"))


def _iiif(spec: SourceSpec) -> SourceAdapter:
    """A IIIF volume — the lane the medallion owns today and gives up at A12.

    Deliberately reuses `storage.iiif`'s existing fetch + retry policy rather than re-deriving it:
    the sequential per-page fetch was the medallion's performance defect, but its RETRY policy was
    sound and is the part worth keeping.
    """
    from storage.iiif import IIIFCachedSource

    volume = str(spec.options.get("volume_id") or "")
    if not volume:
        raise ValueError("iiif source requires options.volume_id")
    return IIIFCachedSource(volume_id=volume, base_url=str(spec.options.get("base_url") or ""))


def _iiif_lineage(spec: SourceSpec) -> LineageInput:
    """R23: the INPUT is the external world — `iiif://…` — never a governed tier."""
    return LineageInput(namespace="iiif", name=str(spec.options.get("volume_id") or ""))


def register_builtin_sources() -> None:
    """Idempotent: safe to call from the app factory and from a test."""
    from ingest.sources import registered_kinds

    known = set(registered_kinds())
    for kind, build, lineage in (
        ("local-dir", _local_dir, _local_dir_lineage),
        ("s3-prefix", _s3_prefix, _s3_prefix_lineage),
        ("iiif", _iiif, _iiif_lineage),
    ):
        if kind not in known:
            register(kind, build=build, lineage_input=lineage)


register_builtin_sources()
