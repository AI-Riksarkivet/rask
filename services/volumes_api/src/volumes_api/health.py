from fastapi import APIRouter
from pydantic import BaseModel

from service_kit.dependencies import SettingsDep


class Health(BaseModel):
    status: str
    input: str
    output: str


router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: SettingsDep) -> Health:
    return Health(status="ok", input=settings.viewer_input, output=settings.viewer_output)
