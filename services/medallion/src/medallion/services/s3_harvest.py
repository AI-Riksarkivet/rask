"""External object-storage harvest — the SECOND external-raw source family (R23).

External raw spans several source families, and each is now ONE registry entry in the ingest
plane rather than a head route here (open_ingest.md I1). This module is the S3 half of that:
``S3PrefixSource`` plus the ``s3_input()`` lineage twin, which sat here unit-tested against moto
with NO ROUTE WIRED for months — precisely because reaching it meant adding another head.

STILL UNWIRED, and this docstring claimed otherwise. It said "It is registered by ``ingest.adapters``
now", which is not what happened: the ingest plane's ``s3-prefix`` adapter registers
``service_kit.lakehouse.sources.S3FileSystemSource`` directly (``ingest/adapters.py``), reaching this module
only in prose. Nothing imports ``S3PrefixSource`` outside ``tests/unit/test_s3_harvest.py``, so the
"registered now" sentence turned a still-open gap into a closed one on paper — the exact failure mode
the module's own history is about. Either point the registry entry here or delete the module; leaving
it described as wired is the one option that costs a reader time.
"""

from __future__ import annotations

from collections.abc import Iterator

from service_kit.lakehouse.sources import SourceObject
from storage import S3Source


class S3PrefixSource:
    """A ``SourceAdapter`` over one external ``s3://<bucket>/<prefix>`` — each object's bytes + s3 URI.

    Wraps :class:`storage.S3Source` (the harvest library, exactly as the image-API reader survives in
    ``storage``): keys iterate in listing order and are re-sorted here so the positional bronze
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
