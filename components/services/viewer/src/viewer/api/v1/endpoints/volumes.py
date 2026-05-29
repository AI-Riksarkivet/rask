"""Image + ALTO endpoints — thin wrappers over `services/volumes.py`."""

from fastapi import APIRouter
from fastapi.responses import Response

from viewer.api.dependencies import SettingsDep
from viewer.schemas.page import PageEntry
from viewer.services import volumes as volumes_service


router = APIRouter(prefix="/volumes", tags=["volumes"])


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
