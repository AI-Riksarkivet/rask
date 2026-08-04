"""Governed-auth settings: the OIDC + OpenFGA knobs any service that authorizes needs.

Extracted so a second service does not become a second copy. `services/catalog` still declares these
inline (`catalog/core/config.py`) — it predates this module and adopting it there is a separate,
test-heavy change; the **aliases here are byte-identical to catalog's**, so one set of `LANCE_*`
environment variables configures every service that mixes this in. Adding a field here without adding
it there would silently split the contract, so keep them in step until catalog adopts this.

The validator encodes the one invariant that must never be relaxed: **authorization requires
authentication**. FGA answers "may THIS subject do it", so enabling it without OIDC would mean
checking a subject nobody verified.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator


class GovernedAuthSettings:
    """Mixin adding the OIDC/FGA knobs. Mix into a `pydantic-settings` `BaseSettings` subclass.

    Deliberately a plain mixin rather than a `BaseSettings` of its own: a service's settings class is
    already a `BaseSettings` with its own env prefix and `model_config`, and inheriting a second one
    would fight over that config.
    """

    oidc_enabled: bool = Field(default=False, alias="LANCE_OIDC_ENABLED")
    oidc_issuer: str | None = Field(default=None, alias="LANCE_OIDC_ISSUER")
    oidc_audience: str | None = Field(default=None, alias="LANCE_OIDC_AUDIENCE")
    #: Split-horizon fetch (reverse-proxied IdP): pull discovery/JWKS in-cluster while tokens keep the
    #: PUBLIC issuer string. Unset = derive the fetch URL from the issuer.
    oidc_discovery_url: str | None = Field(default=None, alias="LANCE_OIDC_DISCOVERY_URL")
    oidc_cache_ttl: int = Field(default=3600, alias="LANCE_OIDC_CACHE_TTL")
    oidc_leeway: int = Field(default=60, alias="LANCE_OIDC_LEEWAY")
    oidc_allow_insecure: bool = Field(default=False, alias="LANCE_OIDC_ALLOW_INSECURE")

    fga_enabled: bool = Field(default=False, alias="LANCE_FGA_ENABLED")
    fga_api_url: str = Field(default="http://openfga:8080", alias="LANCE_FGA_API_URL")
    fga_store_id: str | None = Field(default=None, alias="LANCE_FGA_STORE_ID")
    fga_model_id: str | None = Field(default=None, alias="LANCE_FGA_MODEL_ID")
    fga_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LANCE_FGA_TIMEOUT_SECONDS")
    #: The estate's root FGA object — the one a platform-wide privilege is checked against, as
    #: opposed to a per-tenant one. Shared here rather than redeclared per service because it is a
    #: coordinate every governed service must agree on: the catalog gates `GET /v1/events` on it, the
    #: reconciler excludes it from the ghost report, the chart's bootstrap-admin job seeds the grant
    #: on it, and the viewer now gates its object browser on it (#90). Three copies of the same
    #: default in three configs is exactly how one of them ends up naming a different object and
    #: quietly authorizing against something nobody grants.
    fga_root_object: str = Field(default="warehouse:lance_catalog", alias="LANCE_FGA_ROOT_OBJECT")

    @model_validator(mode="after")
    def _validate_governed_auth(self) -> Self:
        """Fail fast at construction rather than fail open at request time."""
        if self.oidc_enabled and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError("LANCE_OIDC_ISSUER and LANCE_OIDC_AUDIENCE are required when OIDC is enabled")
        if self.fga_enabled and not self.oidc_enabled:
            raise ValueError("LANCE_OIDC_ENABLED is required when LANCE_FGA_ENABLED is set (authz needs a verified subject)")
        return self
