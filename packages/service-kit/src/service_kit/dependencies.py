"""Light DI types shared across services. Resource-specific deps (S3, Lance,
Ray, DB session) stay with their owning services — keep this module free of
lancedb/ray/sqlmodel imports so dependents don't inherit those."""

from typing import Annotated

from fastapi import Depends, Request

from service_kit.config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
