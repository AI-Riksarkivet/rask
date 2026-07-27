"""Per-service settings — the shared data-plane config plus annotator-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (ANNOTATOR_*) are prefixed.
"""

from functools import lru_cache

from common.core.config import Settings
from pydantic import Field


class AnnotatorSettings(Settings):
    service_name: str = "annotator"
    service_port: int = Field(default=8103, alias="ANNOTATOR_PORT")


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
