"""External object-storage harvest — the SECOND external-raw source family (R23).

External raw spans (at least) two source families: the IIIF Image API
(:class:`medallion.services.iiif_produce.IIIFVolumeSource`) and **external object storage** — the ra-hcp
pattern, read through ``packages/storage``'s provider-agnostic S3 client (HCP ↔ MinIO/RustFS/AWS swaps by
env only, never a code change). This module is that adapter seam: :class:`S3PrefixSource` wraps
``storage.S3Source`` behind the ``SourceAdapter`` protocol so ``ingest_to_bronze`` lands the objects as
the bronze blob-v2 dataset, and :func:`s3_input` names the external source on the bronze-write
OpenLineage event (``(s3://<bucket>, <prefix>)`` — the OpenLineage external-dataset naming convention,
the twin of ``iiif_produce.iiif_input``).

A dedicated producer head route (``POST /ingest-s3`` — config, token auth, project routing, symmetric
with ``/ingest-iiif``) is the recorded follow-up; see ``docs/OPEN-WORK.md``.
"""

from __future__ import annotations

from collections.abc import Iterator

from service_kit.lakehouse.sources import SourceObject
from storage import S3Source


class S3PrefixSource:
    """A ``SourceAdapter`` over one external ``s3://<bucket>/<prefix>`` — each object's bytes + s3 URI.

    Wraps :class:`storage.S3Source` (the harvest library, exactly like the IIIF reader survives in
    ``storage.iiif``): keys iterate in listing order and are re-sorted here so the positional bronze
    ``id`` is reproducible — same prefix -> same ids -> same rows every harvest.
    """

    def __init__(self, source: S3Source) -> None:
        self._source = source

    def iter_objects(self) -> Iterator[SourceObject]:
        for key in sorted(self._source.keys()):
            yield SourceObject(uri=f"s3://{self._source.bucket}/{key}", data=self._source.read(key))


def s3_input(bucket: str, prefix: str) -> tuple[str, str]:
    """The EXTERNAL-source input dataset for an object-storage harvest: ``(s3://<bucket>, <prefix>)``.

    Scheme://authority namespace + in-source name, so the graph records which external prefix a bronze
    dataset was harvested from without pretending the source bucket is a Lance dataset (R23: raw is the
    external world, never a catalog tier).
    """
    return (f"s3://{bucket}", prefix.strip("/") or "/")
