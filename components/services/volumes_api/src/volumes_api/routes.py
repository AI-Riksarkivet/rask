"""Image + ALTO endpoints — thin wrappers over `service.py`."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response

from service_kit.dependencies import SettingsDep
from volumes_api import service as volumes_service
from volumes_api.schemas import PageEntry, S3Listing, S3ObjectHead


router = APIRouter(prefix="/volumes", tags=["volumes"])

# The two fixed rask buckets (input images + derived ALTO); mirrors `BUCKETS` in storage.ts.
Bucket = Literal["images-batch", "images-batch-alto"]


@router.get("/objects")
def list_objects(
    settings: SettingsDep,
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    prefix: Annotated[str, Query(description='Key prefix to list under (delimiter "/").')] = "",
) -> S3Listing:
    """List one delimiter-scoped level of `bucket`/`prefix` for the S3 storage browser."""
    return volumes_service.list_objects(settings, bucket, prefix)


@router.get("/object")
def head_object(
    settings: SettingsDep,
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    key: Annotated[str, Query(description="Full object key to describe.")],
) -> S3ObjectHead:
    """Metadata (size/content-type/last-modified/etag) for a single object (404 if missing)."""
    return volumes_service.head_object(settings, bucket, key)


@router.get("/object/download")
def download_object(
    settings: SettingsDep,
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    key: Annotated[str, Query(description="Full object key to download.")],
) -> Response:
    """Return a single object's bytes with a download disposition (404 if missing)."""
    data, content_type = volumes_service.read_object(settings, bucket, key)
    filename = key.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{vol}/pages")
def list_pages(vol: str, settings: SettingsDep) -> list[PageEntry]:
    """List pages under a volume with an `hasAlto` flag derived from the output bucket."""
    return volumes_service.list_pages(settings, vol)


@router.get("/{vol}/pages/{key:path}/image")
def get_image(vol: str, key: str, settings: SettingsDep) -> Response:
    """Stream the source image for `key` (404 if missing; 400 if not under `{vol}/`)."""
    data = volumes_service.read_image(settings, vol, key)
    return Response(content=data, media_type=volumes_service.image_mime(key))


@router.get("/{vol}/pages/{key:path}/alto")
def get_alto(vol: str, key: str, settings: SettingsDep) -> Response:
    """Return ALTO XML for the image `key` (404 if no transcription exists yet)."""
    data = volumes_service.read_alto(settings, vol, key)
    return Response(content=data, media_type="application/xml")
