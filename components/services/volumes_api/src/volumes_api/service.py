"""Volume page listing + image/ALTO reads, on top of the storage package.

Endpoints stay thin: they validate the path then delegate here. `build_source`
picks the concrete `Source` impl from the configured URI scheme (s3://, fs://,
path), so this module never touches boto3 directly.
"""

from service_kit.config import Settings
from service_kit.exceptions import NotFoundError, ValidationError
from storage import build_source, s3_client
from volumes_api.schemas import PageEntry, S3Listing, S3Object, S3ObjectHead


_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


def image_mime(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _IMAGE_MIME.get(ext, "application/octet-stream")


def _require_under_volume(vol: str, key: str) -> None:
    if not key.startswith(vol + "/"):
        raise ValidationError(f"key {key!r} not under volume {vol!r}")


def list_pages(settings: Settings, vol: str) -> list[PageEntry]:
    """Pages under a volume with an `hasAlto` flag derived from the output bucket."""
    prefix = vol.rstrip("/") + "/"
    images_src = build_source(settings.viewer_input, s3_endpoint=settings.s3_endpoint_url, prefix=prefix)
    altos_src = build_source(settings.viewer_output, s3_endpoint=settings.s3_endpoint_url, prefix=prefix, suffixes=(".xml",))
    alto_keys = frozenset(altos_src.keys())
    return [PageEntry(key=key, hasAlto=key.rsplit(".", 1)[0] + ".xml" in alto_keys) for key in sorted(images_src.keys())]


def read_image(settings: Settings, vol: str, key: str) -> bytes:
    """Source image bytes for `key`. `key` must be under `{vol}/` (path-traversal guard)."""
    _require_under_volume(vol, key)
    src = build_source(settings.viewer_input, s3_endpoint=settings.s3_endpoint_url)
    # storage.Source.read can raise FileNotFoundError (FSSource), botocore
    # ClientError (S3Source), or httpx errors (IIIFCachedSource on cold cache).
    # The viewer rule is "no reach into boto3 / botocore", so we catch broadly
    # at this boundary and treat any read failure as 404. TODO(storage): expose
    # a `storage.NotFoundError` so this can narrow without piercing the package.
    try:
        return src.read(key)
    except Exception as exc:
        raise NotFoundError(f"image not found: {key}") from exc


def list_objects(settings: Settings, bucket: str, prefix: str = "") -> S3Listing:
    """One delimiter-scoped level of `bucket`/`prefix` — `prefixes` are sub-"folders", `objects` the leaf keys.

    Uses `storage.s3_client` directly (not `build_source`) because the page-listing
    sources don't expose `Delimiter`/`CommonPrefixes`. Sync boto call — the route is
    `def`, so FastAPI runs it in a threadpool.
    """
    client = s3_client(settings.s3_endpoint_url)
    paginator = client.get_paginator("list_objects_v2")
    prefixes: list[str] = []
    objects: list[S3Object] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        prefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
        for obj in page.get("Contents", []):
            # Skip the prefix's own placeholder key (e.g. the "folder" marker).
            if obj["Key"] == prefix:
                continue
            last_modified = obj.get("LastModified")
            objects.append(
                S3Object(
                    key=obj["Key"],
                    size=obj["Size"],
                    last_modified=last_modified.isoformat() if last_modified is not None else None,
                )
            )
    return S3Listing(bucket=bucket, prefix=prefix, prefixes=prefixes, objects=objects)


def head_object(settings: Settings, bucket: str, key: str) -> S3ObjectHead:
    """Metadata for a single object via S3 HEAD (404 if missing).

    Uses `storage.s3_client` directly (same as `list_objects`) — the page sources
    don't expose a HEAD. Sync boto call; the route is `def`, so FastAPI threadpools it.
    """
    client = s3_client(settings.s3_endpoint_url)
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        # Broad catch at the storage boundary (the viewer rule is "no reach into
        # boto/botocore") — a missing key or any read failure is a 404.
        raise NotFoundError(f"object not found: {bucket}/{key}") from exc
    last_modified = resp.get("LastModified")
    etag = (resp.get("ETag") or "").strip('"')
    return S3ObjectHead(
        key=key,
        size=resp["ContentLength"],
        content_type=resp.get("ContentType"),
        last_modified=last_modified.isoformat() if last_modified is not None else None,
        etag=etag or None,
    )


def read_object(settings: Settings, bucket: str, key: str) -> tuple[bytes, str]:
    """Full object bytes + content-type for download/preview (404 if missing).

    Reads fully into memory like `read_image`/`read_alto`: the two rask buckets hold
    page images (~MBs) and ALTO XML (small), so streaming isn't worth the boto3
    client-lifetime juggling. Returns `(data, content_type)`.
    """
    client = s3_client(settings.s3_endpoint_url)
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise NotFoundError(f"object not found: {bucket}/{key}") from exc
    return resp["Body"].read(), resp.get("ContentType") or "application/octet-stream"


def read_alto(settings: Settings, vol: str, key: str) -> bytes:
    """ALTO XML for the image `key` (404 if no transcription exists yet)."""
    _require_under_volume(vol, key)
    alto_key = key.rsplit(".", 1)[0] + ".xml"
    src = build_source(settings.viewer_output, s3_endpoint=settings.s3_endpoint_url, suffixes=(".xml",))
    try:
        return src.read(alto_key)
    except Exception as exc:
        raise NotFoundError(f"ALTO not found: {alto_key}") from exc
