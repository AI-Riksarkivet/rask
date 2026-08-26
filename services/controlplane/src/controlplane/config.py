"""The controlplane's settings, and the reason it has its own class at all.

It rode `service_kit`'s generic `Settings` — which carries the shared knobs (api prefix, CORS, OTel)
and NONE of the estate's auth knobs. The consequence was not "auth is off here" but "auth CANNOT be
turned on here": with no `GovernedAuthSettings` there is no `LANCE_OIDC_ENABLED` to bind, so no chart
value could gate `GET /api/projects/` however the estate was configured. That route returns every
operator Project CR in the cluster — slug, team, workload type, k8s namespace and each tenant's live
ingress host — and `gateway/__init__.py` carries it to the public edge.

The mixin is additive and every field defaults OFF, so a stack that sets none of them behaves exactly
as before this file existed.
"""

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
