"""Image + ALTO proxies. Source URIs come from settings; the storage package
builds the right `Source` impl based on the URI scheme (s3://, fs://, path)."""

from fastapi import APIRouter
from fastapi.responses import Response

from storage import build_source
from viewer.api.dependencies import SettingsDep
from viewer.core.exceptions import NotFoundError, ValidationError
from viewer.schemas.page import PageEntry


router = APIRouter(prefix="/volumes", tags=["volumes"])


_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


def _image_mime(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _IMAGE_MIME.get(ext, "application/octet-stream")


def _require_under_volume(vol: str, key: str) -> None:
    if not key.startswith(vol + "/"):
        raise ValidationError(f"key {key!r} not under volume {vol!r}")


@router.get("/{vol}/pages")
def list_pages(vol: str, settings: SettingsDep) -> list[PageEntry]:
    """List pages under a volume with an `hasAlto` flag derived from the output bucket."""
    prefix = vol.rstrip("/") + "/"
    images_src = build_source(settings.viewer_input, s3_endpoint=settings.hcp_endpoint, prefix=prefix)
    altos_src = build_source(settings.viewer_output, s3_endpoint=settings.hcp_endpoint, prefix=prefix, suffixes=(".xml",))
    alto_keys = frozenset(altos_src.keys())
    return [PageEntry(key=key, hasAlto=key.rsplit(".", 1)[0] + ".xml" in alto_keys) for key in sorted(images_src.keys())]


@router.get("/{vol}/pages/{key:path}/image")
def get_image(vol: str, key: str, settings: SettingsDep) -> Response:
    """Stream the source image for `key`. `key` must be under `{vol}/` (path-traversal guard)."""
    _require_under_volume(vol, key)
    src = build_source(settings.viewer_input, s3_endpoint=settings.hcp_endpoint)
    try:
        data = src.read(key)
    except Exception as exc:
        raise NotFoundError(f"image not found: {key}") from exc
    return Response(content=data, media_type=_image_mime(key))


@router.get("/{vol}/pages/{key:path}/alto")
def get_alto(vol: str, key: str, settings: SettingsDep) -> Response:
    """Return ALTO XML for the image `key` (404 if no transcription exists yet)."""
    _require_under_volume(vol, key)
    alto_key = key.rsplit(".", 1)[0] + ".xml"
    src = build_source(settings.viewer_output, s3_endpoint=settings.hcp_endpoint, suffixes=(".xml",))
    try:
        data = src.read(alto_key)
    except Exception as exc:
        raise NotFoundError(f"ALTO not found: {alto_key}") from exc
    return Response(content=data, media_type="application/xml")
