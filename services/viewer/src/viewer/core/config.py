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

    #: The Dapr secret-store COMPONENT the per-store S3 credentials are read from (`objects.py`).
    #:
    #: Declared here rather than read where it is used (VS-24): a bare env read for
    #: `RASK_SECRET_STORE` inside an endpoint module was the only such read in the service, and an undeclared
    #: setting has no type, no discoverable default and no place a reader looks. The env NAME is kept
    #: so no deployment moves. It names the same component as `s3_secret_store` (MEDIA_S3_SECRET_STORE)
    #: does for the warehouse's own bundle; the two variables are a duplication this service inherited
    #: from `services/ingest`, and collapsing them is a config-contract change, not a rename.
    secret_store: str = Field(default="lance-secrets", alias="RASK_SECRET_STORE")
    #: Where the voiceprint runner's Ray Serve app answers. EMPTY DISABLES the upload form, and that
    #: is the honest default: the read plane no longer carries a speaker encoder, so an estate that
    #: has not deployed the runner cannot embed — and saying so at the door beats a 500 from a model
    #: that is not there. The Lance-anchored GET forms are unaffected; they run no encoder.
    #:
    #: A setting rather than a literal because a hard-coded endpoint does not survive multi-env
    #: deploys (`fastapi` references/anti-patterns.md).
    voiceprint_serve_url: str = Field(default="", alias="VIEWER_VOICEPRINT_SERVE_URL")

    #: Where `media_clip`'s ffmpeg fetches the source media from. EMPTY derives the loopback from
    #: `service_port` — see :attr:`clip_source_origin`.
    #:
    #: A setting, because the alternative was the REQUEST (VS-09): the origin used to be
    #: `request.base_url`, which is the caller's `Host` / `X-Forwarded-Host` header, so a request
    #: could point the viewer's own ffmpeg at any host the pod can reach and download the transcode
    #: of whatever answered. Configuration is the only source for a URL this service fetches on its
    #: own behalf.
    clip_source_origin_override: str = Field(default="", alias="VIEWER_CLIP_SOURCE_ORIGIN")

    @property
    def clip_source_origin(self) -> str:
        """The trusted origin ffmpeg pulls clip source bytes from.

        `127.0.0.1` rather than `host`: this is a call the pod makes to ITSELF, and `host` is the
        BIND address (`0.0.0.0` in the chart), which is not an address to connect to. The override
        exists for a deployment that fronts the media route somewhere else.
        """
        return self.clip_source_origin_override or f"http://127.0.0.1:{self.service_port}"


@lru_cache
def get_viewer_settings() -> ViewerSettings:
    return ViewerSettings()
