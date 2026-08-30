"""Governed-auth settings: the OIDC + OpenFGA knobs, declared ONCE for the whole estate.

THE DEFECT THIS MODULE NOW CLOSES. Until 2026-08-30 the same field-set was declared FIVE times under
FOUR prefixes — this mixin and the catalog's inline twin under `LANCE_*`, lineage's under `LINEAGE_*`,
maintenance's under `MAINTENANCE_*`, medallion's under `MEDALLION_*`. Turning authorization on
estate-wide therefore meant setting four different names for one switch (the chart carried a
prefix-PARAMETERISED helper whose only reason to exist was that disagreement), and the copies had
already drifted in both directions: medallion declared `fga_store_id: str = ""` where every other copy
declared `str | None`, dropped the `ge=0.1` floor on `fga_timeout_seconds`, and — with lineage —
never grew the HTTPS-issuer validator, so an `http://` issuer booted there and answered 401 on every
VALID bearer.

One declaration, one `RASK_*` name each. The old prefixed names are DELETED, with no alias list and no
precedence chain: `_refuse_retired_names` turns a deployment that still sets one into a startup error,
because the failure it replaces — half an estate authenticating and half not — is silent.

THREE CLASSES, because the estate really does have three postures:

* `FgaSettings` — the OpenFGA client alone. A service that authorizes as its OWN service identity and
  has no human door (`maintenance`'s reconciler; medallion's movers) needs the client and no verifier.
* `OidcSettings` — the bearer-token verifier alone, with the invariant that a knowable
  misconfiguration must not become a runtime 401.
* `GovernedAuthSettings` — both, plus the coupling: FGA answers "may THIS subject do it", so a service
  with a human door must not authorize a subject nobody authenticated.

Mix into a `pydantic-settings` `BaseSettings` subclass. Deliberately plain mixins rather than
`BaseSettings` of their own: a service's settings class is already a `BaseSettings` with its own
`model_config`, and inheriting a second one would fight over that config.
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Self

from dotenv import dotenv_values
from pydantic import Field, model_validator


#: The names the 2026-08-30 hard rename deleted. Kept as a REFUSAL list, never as an alias list: an
#: operator who upgrades without re-templating must be stopped at boot, not silently unauthenticated.
#: Service-specific FGA knobs (`LANCE_FGA_CASCADE_WRITERS`, `LANCE_FGA_LOCK_ROOT_CREATE`,
#: `LINEAGE_FGA_OBJECT_TYPE`, `MEDALLION_FGA_SERVICE_IDENTITY`, `MEDALLION_FGA_REQUIRED_ACTION`) are
#: absent on purpose — each is declared once already and keeps its owning service's namespace.
RETIRED_AUTH_ENV_NAMES: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("LANCE", "LINEAGE", "MEDALLION", "MAINTENANCE")
    for suffix in (
        "OIDC_ENABLED",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_DISCOVERY_URL",
        "OIDC_CACHE_TTL",
        "OIDC_LEEWAY",
        "OIDC_ALLOW_INSECURE",
        "FGA_ENABLED",
        "FGA_API_URL",
        "FGA_STORE_ID",
        "FGA_MODEL_ID",
        "FGA_TIMEOUT_SECONDS",
        "FGA_ROOT_OBJECT",
    )
)


class FgaSettings:
    """The OpenFGA client coordinates. Mix in alone when the service authorizes as ITSELF."""

    fga_enabled: bool = Field(default=False, alias="RASK_FGA_ENABLED")
    fga_api_url: str = Field(default="http://openfga:8080", alias="RASK_FGA_API_URL")
    fga_store_id: str | None = Field(default=None, alias="RASK_FGA_STORE_ID")
    fga_model_id: str | None = Field(default=None, alias="RASK_FGA_MODEL_ID")
    fga_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="RASK_FGA_TIMEOUT_SECONDS")
    #: The estate's root FGA object — the one a platform-wide privilege is checked against, as
    #: opposed to a per-tenant one. Shared here rather than redeclared per service because it is a
    #: coordinate every governed service must agree on: the catalog gates `GET /v1/events` on it, the
    #: reconciler excludes it from the ghost report, the chart's bootstrap-admin job seeds the grant
    #: on it, and the viewer gates its object browser on it (#90). Three copies of the same default in
    #: three configs is exactly how one of them ends up naming a different object and quietly
    #: authorizing against something nobody grants.
    fga_root_object: str = Field(default="warehouse:lance_catalog", alias="RASK_FGA_ROOT_OBJECT")

    @model_validator(mode="before")
    @classmethod
    def _refuse_retired_names(cls, data: object) -> object:
        """A retired variable is an ERROR, not a no-op.

        Every one of these classes sets `populate_by_name` with a service `env_prefix`, which teaches
        pydantic-settings the bare FIELD NAME as a second lookup. So a service-prefixed spelling of
        one of these switches would still BIND on the class carrying that prefix — and bind nothing
        anywhere else. That is the pre-rename estate wearing new names: one switch set under four
        spellings, disagreeing pod by pod. Refusing the name outright is the only version of
        "deleted" that a running deployment can observe.

        (The retired spellings are deliberately not written out here. `RETIRED_AUTH_ENV_NAMES` builds
        them from prefix-by-suffix precisely so no literal survives in the tree, which is what lets
        `test_the_retired_names_are_gone_from_the_repository` scan every tracked file — prose
        included — without needing an exemption list to be argued over.)

        SCANS BOTH SOURCES pydantic-settings would read: the process environment AND the configured
        `env_file`. It checked only `os.environ` at first, justified by "every deployment path in this
        estate sets real env vars" — but four settings classes DO set `env_file=".env"`
        (`ingest.auth`, `controlplane`, `gateway`, `flows`), and `ingest.auth` pairs it with
        `env_prefix="LANCE_"`. A retired name in a dotenv therefore bound silently while the guard
        stayed quiet, which is the one outcome a hard rename exists to prevent. Sources, not
        assembled values: by the time pydantic-settings hands over a dict, the NAME that supplied a
        value is gone.
        """
        if retired := cls._retired_names_in_scope():
            raise ValueError(
                f"{', '.join(retired)} no longer exist — the auth settings were unified onto one "
                f"RASK_* name each (RASK_OIDC_*, RASK_FGA_*). Re-template the deployment; there is no "
                f"alias and no fallback, so leaving these set would authenticate some pods and not others."
            )
        return data

    @classmethod
    def _retired_names_in_scope(cls) -> list[str]:
        """Every retired name set on any source this class would actually read.

        Case-insensitive, because several of these classes set `case_sensitive=False` and a dotenv
        written in lower case binds exactly the same — a guard that missed it would be precisely as
        silent as no guard.
        """
        retired_by_upper = {name.upper(): name for name in RETIRED_AUTH_ENV_NAMES}
        found = {retired_by_upper[key.upper()] for key in os.environ if key.upper() in retired_by_upper}
        for path in _configured_env_files(cls):
            with suppress(OSError):
                found |= {retired_by_upper[key.upper()] for key in dotenv_values(path) if key.upper() in retired_by_upper}
        return sorted(found)


def _configured_env_files(cls: type) -> list[Path]:
    """The dotenv paths a settings class declares, normalised. Empty for a class that declares none.

    `env_file` accepts a str, a Path, or a sequence of either, so all three shapes are handled here
    rather than at the one call site — the alternative is a guard that works on three of the four
    classes and reads as if it works on all of them.
    """
    declared = getattr(cls, "model_config", {}).get("env_file")
    if declared is None:
        return []
    candidates = [declared] if isinstance(declared, str | Path) else list(declared)
    return [Path(entry) for entry in candidates if entry]


class OidcSettings:
    """The bearer-token verifier's knobs. Mix in alone only alongside `FgaSettings` (see medallion)."""

    oidc_enabled: bool = Field(default=False, alias="RASK_OIDC_ENABLED")
    oidc_issuer: str | None = Field(default=None, alias="RASK_OIDC_ISSUER")
    oidc_audience: str | None = Field(default=None, alias="RASK_OIDC_AUDIENCE")
    #: Split-horizon fetch (reverse-proxied IdP): pull discovery/JWKS in-cluster while tokens keep the
    #: PUBLIC issuer string. Unset = derive the fetch URL from the issuer.
    oidc_discovery_url: str | None = Field(default=None, alias="RASK_OIDC_DISCOVERY_URL")
    oidc_cache_ttl: int = Field(default=3600, alias="RASK_OIDC_CACHE_TTL")
    oidc_leeway: int = Field(default=60, alias="RASK_OIDC_LEEWAY")
    oidc_allow_insecure: bool = Field(default=False, alias="RASK_OIDC_ALLOW_INSECURE")

    @model_validator(mode="after")
    def _validate_oidc(self) -> Self:
        """Fail fast at construction rather than fail open — or fail confusingly — at request time."""
        if self.oidc_enabled and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError("RASK_OIDC_ISSUER and RASK_OIDC_AUDIENCE are required when OIDC is enabled")
        # AN `http://` ISSUER IS A MISCONFIGURATION, AND IT BELONGS HERE. `_require_https` catches it at
        # VERIFY time and raises `UnauthenticatedError`, which every call site maps to 401 "Invalid or
        # expired token" — so an operator who shipped `RASK_OIDC_ISSUER=http://…` got a service where
        # every VALID bearer answered 401 with the body a genuinely expired token gets, indistinguishable
        # to the caller. A scheme is knowable at construction, which is what this validator is for.
        #
        # The `jwks_uri` half cannot move here: it comes from DISCOVERY, so settings never sees it and
        # `_require_https` remains the backstop for that path.
        if self.oidc_enabled and self.oidc_issuer and not self.oidc_allow_insecure and not self.oidc_issuer.startswith("https://"):
            raise ValueError("RASK_OIDC_ISSUER must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for a dev IdP)")
        return self


class GovernedAuthSettings(OidcSettings, FgaSettings):
    """Both halves plus the coupling — what a service with a HUMAN door mixes in.

    The coupling lives here and not on `FgaSettings` because it is a statement about human doors:
    FGA answers "may THIS subject do it", so enabling it without OIDC would mean checking a subject
    nobody verified. A service that authorizes as its own SERVICE identity has already answered
    "which subject" from config, so it composes the two mixins directly instead (medallion), or takes
    `FgaSettings` alone (maintenance).
    """

    @model_validator(mode="after")
    def _authorization_requires_authentication(self) -> Self:
        if self.fga_enabled and not self.oidc_enabled:
            raise ValueError("RASK_OIDC_ENABLED is required when RASK_FGA_ENABLED is set (authz needs a verified subject)")
        return self
