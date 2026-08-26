"""The controlplane's settings dependency — the seam `make_auth_deps` binds against."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from controlplane.config import ControlplaneSettings


@lru_cache(maxsize=1)
def get_controlplane_settings() -> ControlplaneSettings:
    """Built once. `lru_cache` on a module-level function, never on a method (writing-python)."""
    return ControlplaneSettings()


ControlplaneSettingsDep = Annotated[ControlplaneSettings, Depends(get_controlplane_settings)]
