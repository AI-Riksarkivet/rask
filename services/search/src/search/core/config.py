"""Per-service settings — the shared data-plane config plus search-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (SEARCH_*) are prefixed.
"""

from functools import lru_cache

from pydantic import Field

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.media.config import Settings


class SearchSettings(Settings, GovernedAuthSettings):
    """The search service's config, now carrying the shared OIDC/FGA knobs.

    It shipped with NO authorization of any kind — the only explorer service without one — while the
    chart set `LANCE_OIDC_*`/`LANCE_FGA_*` on all three. That env reached this service and bound to
    nothing, so authorization here was configured-looking and inert (open_python-audit X6), and the
    route that takes a raw SQL `where` predicate was the one with no subject behind it (VS-13).

    Mixed in rather than declared as SEARCH_* twins, for the reason ViewerSettings gives: one set of
    variables across every governed service, and the shared fail-fast validator (FGA without OIDC is
    refused at construction, because authz needs a verified subject to be about).
    """

    service_name: str = "search"
    service_port: int = Field(default=8102, alias="SEARCH_PORT")


@lru_cache
def get_search_settings() -> SearchSettings:
    return SearchSettings()
