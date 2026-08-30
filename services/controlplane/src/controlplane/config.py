"""The controlplane's settings, and the reason it has its own class at all.

It rode `service_kit`'s generic `Settings` — which carries the shared knobs (api prefix, CORS, OTel)
and NONE of the estate's auth knobs. The consequence was not "auth is off here" but "auth CANNOT be
turned on here": with no `GovernedAuthSettings` there is no `RASK_OIDC_ENABLED` to bind, so no chart
value could gate `GET /api/projects/` however the estate was configured. That route returns every
operator Project CR in the cluster — slug, team, workload type, k8s namespace and each tenant's live
ingress host — and `gateway/__init__.py` carries it to the public edge.

The mixin is additive and every field defaults OFF, so a stack that sets none of them behaves exactly
as before this file existed.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings


class ControlplaneSettings(GovernedAuthSettings, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
        # every `LANCE_*` alias on the mixin silently gains an un-namespaced twin — `FGA_ENABLED`
        # would turn authorization off here. `env_prefix` redirects that fallback onto the namespace
        # the aliases already declare; an explicit alias bypasses it, so deliberately-bare names
        # still land. The same pair `FlowsSettings` carries, and the reason
        # `tests/unit/test_settings_env_namespace.py` exists.
        populate_by_name=True,
        env_prefix="LANCE_",
    )

    #: The scheme of each project's entry URL — `f"{scheme}://{host}/overview"`, which the home
    #: zone's gallery renders as the link into a tenant.
    #:
    #: It was `os.environ.get("RASK_PROJECT_URL_SCHEME", "http")` inside the route body, read on
    #: every request and validated by nothing (FLEET-ENV-SCATTER). A value that reaches a rendered
    #: link is not free text: `Literal` makes the only two answers the only two answers, so a typo is
    #: a startup error naming the variable instead of an estate-wide gallery of links nobody can
    #: follow. Explicit alias, because the mixin's `env_prefix="LANCE_"` would otherwise namespace it
    #: away from the `RASK_` name every deployment already uses.
    project_url_scheme: Literal["http", "https"] = Field(default="http", alias="RASK_PROJECT_URL_SCHEME")
