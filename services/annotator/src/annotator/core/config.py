"""Per-service settings — the shared data-plane config plus annotator-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (ANNOTATOR_*) are prefixed.

It also mixes in :class:`GovernedAuthSettings`, because the annotator now writes **per-subject**
state (annotation projects, tasks, claims, drafts) rather than only serving read-plane media. Every
entity in ``OPEN-WORK.md#design--annotation-projects`` is keyed on who owns or claims it, so the service
needs a VERIFIED subject: the trusted ``X-User`` header it used to accept becomes a cross-user leak
the moment anything is keyed on identity.
"""

from functools import lru_cache

from pydantic import Field

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.media.config import Settings


class AnnotatorSettings(GovernedAuthSettings, Settings):
    service_name: str = "annotator"
    service_port: int = Field(default=8103, alias="ANNOTATOR_PORT")


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
