from fastapi import APIRouter

from viewer.api.dependencies import SettingsDep
from viewer.schemas.health import Health


router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: SettingsDep) -> Health:
    return Health(status="ok", input=settings.viewer_input, output=settings.viewer_output)
