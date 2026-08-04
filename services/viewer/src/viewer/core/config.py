"""Per-service settings — the shared data-plane config plus viewer-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (VIEWER_*) are prefixed.
"""

from functools import lru_cache

from pydantic import Field

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.media.config import Settings


class ViewerSettings(Settings, GovernedAuthSettings):
    """The viewer's config, now carrying the shared OIDC/FGA knobs.

    The service shipped with NO authorization at all — `/api/datasets` enumerated every corpus on
    disk, with its table stats and capabilities, to any caller. That was the documented
    "localhost / trusted network" posture, and it stops being defensible the moment the zone is
    reachable by more than one person: a corpus LIST names data someone may not know exists.

    Mixing the shared `GovernedAuthSettings` in rather than declaring VIEWER_* twins keeps one set of
    LANCE_OIDC_*/LANCE_FGA_* variables across every governed service, and inherits its fail-fast
    validator (FGA without OIDC is refused at construction, because authz needs a verified subject).
    """

    service_name: str = "viewer"
    service_port: int = Field(default=8101, alias="VIEWER_PORT")


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
