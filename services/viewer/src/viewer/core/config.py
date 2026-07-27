"""Per-service settings — the shared data-plane config plus viewer-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (VIEWER_*) are prefixed.
"""

from functools import lru_cache

from common.core.config import Settings
from pydantic import Field


class ViewerSettings(Settings):
    service_name: str = "viewer"
    service_port: int = Field(default=8101, alias="VIEWER_PORT")


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
