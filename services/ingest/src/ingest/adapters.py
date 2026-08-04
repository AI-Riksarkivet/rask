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

from ingest.sources import LineageInput, SourceOption, SourceSpec, register


if TYPE_CHECKING:
    from collections.abc import Iterator

    from service_kit.lakehouse.sources import SourceAdapter, SourceObject


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


class IIIFVolumeSource:
    """A IIIF volume as a `SourceAdapter` — the lane the medallion owns today and gives up at A12.

    Wraps `storage.iiif`'s `get_image_ids` / `build_image_url` / `fetch_image`, which is what the
    medallion's producer itself uses (`iiif_produce.py:41`). Deliberately NOT `IIIFCachedSource`:
    despite the name it is a keys+read CACHE (`keys`/`read`/`cached_keys`), it has no `iter_objects`,
    and the medallion's own docstring calls it retired. An earlier version of this file registered it
    anyway, on a signature guessed from a grep — both the signature and the interface were wrong.

    Nothing would have caught that before a live run: `SourceAdapter` is a plain Protocol, NOT
    `runtime_checkable` (an `isinstance` against it raises TypeError), so there is no import-time or
    startup check to fail. The registry would have happily handed back an object with no
    `iter_objects`, and the first enumeration of a real volume would have been the error report.
    `test_adapters.py` therefore asserts the method exists rather than trusting the annotation.

    The retry policy stays `storage.iiif`'s. The medallion's defect was SEQUENTIAL fetching in the
    request path, not its retries; the worker's bounded concurrency fixes the former, and re-deriving
    the latter would only lose hard-won behaviour against a rate-limited endpoint.
    """

    def __init__(self, volume_id: str, iiif_base: str | None = None, query_params: str | None = None) -> None:
        self._volume = volume_id
        self._base = iiif_base
        self._query = query_params

    def iter_objects(self) -> Iterator[SourceObject]:
        from service_kit.lakehouse.sources import SourceObject
        from storage.iiif import fetch_image

        for url in self.iter_keys():
            yield SourceObject(uri=url, data=fetch_image(url))

    def iter_keys(self) -> Iterator[str]:
        """Every page's URL, from the volume manifest alone — no image bytes transferred.

        This is where the keys-without-bytes protocol pays for itself. `get_image_ids` is ONE
        manifest request; building the URLs is string work. Enumerating through `iter_objects`
        instead downloaded the entire volume — multi-MB per page — purely to read back the URL that
        had just been used to fetch it, and then the workers fetched all of it again.
        """
        from storage.iiif import DEFAULT_IIIF_BASE, DEFAULT_QUERY_PARAMS, build_image_url, get_image_ids

        base = self._base or DEFAULT_IIIF_BASE
        query = self._query or DEFAULT_QUERY_PARAMS
        for image_id in get_image_ids(self._volume):
            yield build_image_url(image_id, iiif_base=base, query_params=query)


def _iiif(spec: SourceSpec) -> SourceAdapter:
    volume = str(spec.options.get("volume_id") or "")
    if not volume:
        raise ValueError("iiif source requires options.volume_id")
    base = spec.options.get("iiif_base")
    query = spec.options.get("query_params")
    return IIIFVolumeSource(volume, str(base) if base else None, str(query) if query else None)


def _iiif_lineage(spec: SourceSpec) -> LineageInput:
    """R23: the INPUT is the external world — `iiif://…` — never a governed tier."""
    return LineageInput(namespace="iiif", name=str(spec.options.get("volume_id") or ""))


def register_builtin_sources() -> None:
    """Idempotent: safe to call from the app factory and from a test.

    Each kind declares the `options` it takes right here, beside the factory that reads them — the
    two drift the moment they are apart. `describe_sources()` serves these, so a UI renders the
    fields a kind actually has and nothing in the frontend restates them. That is what keeps adding
    a source a backend-only diff (gate A9): the compute zone's form gained S3-prefix and local-dir
    without a line of Svelte, because it asks.
    """
    from ingest.sources import registered_kinds

    known = set(registered_kinds())

    if "local-dir" not in known:
        register(
            "local-dir",
            build=_local_dir,
            lineage_input=_local_dir_lineage,
            label="Local directory",
            description="A directory tree on the worker. Deterministic and offline — the lane's fixture source.",
            options=[
                SourceOption(name="root", label="Directory", required=True, placeholder="/data/pages", help="Absolute path, readable by the ingest pod."),
                SourceOption(name="pattern", label="Filename pattern", placeholder="*", help="Glob applied within the directory. Defaults to every file."),
            ],
        )

    if "s3-prefix" not in known:
        register(
            "s3-prefix",
            build=_s3_prefix,
            lineage_input=_s3_prefix_lineage,
            label="S3 prefix",
            description="Every object under a bucket prefix, through the estate's provider-agnostic client — RustFS, MinIO, HCP or AWS.",
            options=[
                SourceOption(name="bucket", label="Bucket", required=True, placeholder="lance-catalog"),
                SourceOption(name="prefix", label="Prefix", placeholder="volumes/A0068688/", help="Leave empty to take the whole bucket."),
                SourceOption(
                    name="endpoint",
                    label="Endpoint URL",
                    placeholder="(the configured default)",
                    help="Override only when the bucket is not on the estate's own endpoint.",
                ),
            ],
        )

    if "iiif" not in known:
        register(
            "iiif",
            build=_iiif,
            lineage_input=_iiif_lineage,
            label="IIIF volume",
            description="Every page of a volume from a IIIF Image API. External raw (R23) — the lineage input is the iiif:// source, never a governed tier.",
            options=[
                SourceOption(name="volume_id", label="Volume id", required=True, placeholder="A0068688"),
                SourceOption(
                    name="max_pages",
                    label="Max pages",
                    numeric=True,
                    placeholder="all",
                    help="Stop after this many pages. Leave empty to harvest the whole volume.",
                ),
                SourceOption(name="iiif_base", label="IIIF base URL", placeholder="(the configured default)"),
                SourceOption(
                    name="query_params",
                    label="Image query",
                    placeholder="full/max/0/default.jpg",
                    help="The IIIF Image API parameters appended to each page request.",
                ),
            ],
        )


register_builtin_sources()
