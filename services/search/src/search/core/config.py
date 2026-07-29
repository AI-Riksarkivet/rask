"""Per-service settings — the shared data-plane config plus search-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (SEARCH_*) are prefixed.
"""

from functools import lru_cache

from pydantic import Field

from service_kit.media.config import Settings


class SearchSettings(Settings):
    service_name: str = "search"
    service_port: int = Field(default=8102, alias="SEARCH_PORT")


@lru_cache
def get_search_settings() -> SearchSettings:
    return SearchSettings()
