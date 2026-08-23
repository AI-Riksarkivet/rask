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
    #: Where the voiceprint runner's Ray Serve app answers. EMPTY DISABLES the upload form, and that
    #: is the honest default: the read plane no longer carries a speaker encoder, so an estate that
    #: has not deployed the runner cannot embed — and saying so at the door beats a 500 from a model
    #: that is not there. The Lance-anchored GET forms are unaffected; they run no encoder.
    #:
    #: A setting rather than a literal because a hard-coded endpoint does not survive multi-env
    #: deploys (`fastapi` references/anti-patterns.md).
    voiceprint_serve_url: str = Field(default="", alias="VIEWER_VOICEPRINT_SERVE_URL")


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
