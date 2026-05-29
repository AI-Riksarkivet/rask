"""Volume page listing + image/ALTO reads, on top of the storage package.

Endpoints stay thin: they validate the path then delegate here. `build_source`
picks the concrete `Source` impl from the configured URI scheme (s3://, fs://,
path), so this module never touches boto3 directly.
"""

from storage import build_source
from viewer.core.config import Settings
from viewer.core.exceptions import NotFoundError, ValidationError
from viewer.schemas.page import PageEntry


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
    images_src = build_source(settings.viewer_input, s3_endpoint=settings.hcp_endpoint, prefix=prefix)
    altos_src = build_source(settings.viewer_output, s3_endpoint=settings.hcp_endpoint, prefix=prefix, suffixes=(".xml",))
    alto_keys = frozenset(altos_src.keys())
    return [PageEntry(key=key, hasAlto=key.rsplit(".", 1)[0] + ".xml" in alto_keys) for key in sorted(images_src.keys())]


def read_image(settings: Settings, vol: str, key: str) -> bytes:
    """Source image bytes for `key`. `key` must be under `{vol}/` (path-traversal guard)."""
    _require_under_volume(vol, key)
    src = build_source(settings.viewer_input, s3_endpoint=settings.hcp_endpoint)
    # storage.Source.read can raise FileNotFoundError (FSSource), botocore
    # ClientError (S3Source), or httpx errors (IIIFCachedSource on cold cache).
    # The viewer rule is "no reach into boto3 / botocore", so we catch broadly
    # at this boundary and treat any read failure as 404. TODO(storage): expose
    # a `storage.NotFoundError` so this can narrow without piercing the package.
    try:
        return src.read(key)
    except Exception as exc:
        raise NotFoundError(f"image not found: {key}") from exc


def read_alto(settings: Settings, vol: str, key: str) -> bytes:
    """ALTO XML for the image `key` (404 if no transcription exists yet)."""
    _require_under_volume(vol, key)
    alto_key = key.rsplit(".", 1)[0] + ".xml"
    src = build_source(settings.viewer_output, s3_endpoint=settings.hcp_endpoint, suffixes=(".xml",))
    try:
        return src.read(alto_key)
    except Exception as exc:
        raise NotFoundError(f"ALTO not found: {alto_key}") from exc
