"""Governed-auth settings: the OIDC + OpenFGA knobs, declared ONCE for the whole estate.

ONE DECLARATION FOR THE ESTATE, because per-service copies under per-service prefixes (`LANCE_*`,
`LINEAGE_*`, `MAINTENANCE_*`) make turning authorization on estate-wide mean setting several names for
one switch, and the copies drift: measured before 2026-08-30, one copy declared `fga_store_id: str = ""`
where the rest declared `str | None`, dropped the `ge=0.1` floor on `fga_timeout_seconds`, and never grew
the HTTPS-issuer validator, so an `http://` issuer booted there and answered 401 on every VALID bearer.

One declaration, one `RASK_*` name each. The old prefixed names are DELETED, with no alias list and no
precedence chain: `_refuse_retired_names` turns a deployment that still sets one into a startup error,
because the failure it replaces — half an estate authenticating and half not — is silent.

THREE CLASSES, because the estate really does have three postures:

* `FgaSettings` — the OpenFGA client alone. A service that authorizes as its OWN service identity and
  has no human door (`maintenance`'s reconciler) needs the client and no verifier.
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lakehouse.governed.principal import CLIENT_IDP_SUFFIX, is_machine_idp, require_idp_id


#: The names the 2026-08-30 hard rename deleted. Kept as a REFUSAL list, never as an alias list: an
#: operator who upgrades without re-templating must be stopped at boot, not silently unauthenticated.
#: Service-specific FGA knobs (`LINEAGE_FGA_OBJECT_TYPE`) are
#: absent on purpose — each is declared once already and keeps its owning service's namespace.
RETIRED_AUTH_ENV_NAMES: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}"
    for prefix in ("LANCE", "LINEAGE", "MAINTENANCE")
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
    #: This pod's projected ServiceAccount token for the `rask-openfga` audience, presented as the bearer of
    #: every OpenFGA request and read on each one, because the kubelet rotates it ([[XC-077]]). Unset sends
    #: no credential, which only an OpenFGA without authn admits; the chart sets it wherever auth is on.
    fga_token_file: str | None = Field(default=None, alias="RASK_FGA_TOKEN_FILE")
    #: The catalog whose decision door answers this service's authorization questions ([[LH-442]],
    #: `lakehouse.governed.decider`). Set, the service builds no OpenFGA client and holds no credential to the
    #: store; unset, it is one of the store's clients at `fga_api_url`. The chart sets it on every governed
    #: service that writes no tuple.
    fga_decider_url: str | None = Field(default=None, alias="RASK_FGA_DECIDER_URL")
    #: This pod's projected ServiceAccount token for the catalog's `rask-catalog` audience, the bearer of every
    #: decision request, read on each one.
    fga_decider_token_file: str | None = Field(default=None, alias="RASK_FGA_DECIDER_TOKEN_FILE")
    #: The estate's root FGA object — the one a platform-wide PRIVILEGE is checked against, as
    #: opposed to a per-tenant one. Shared here rather than redeclared per service because it is a
    #: coordinate every governed service must agree on: the catalog gates its estate-wide doors on it,
    #: lineage gates its whole-estate projection on it, and a client gating an estate-wide surface of its
    #: own asks the estate-admin verb there through the decision door. Three
    #: copies of the same default in three configs is exactly how one of them ends up naming a
    #: different object and quietly authorizing against something nobody grants.
    fga_root_object: str = Field(default="estate:rask", alias="RASK_FGA_ROOT_OBJECT")
    #: The estate's default WAREHOUSE — a structural coordinate, and deliberately NOT the one above.
    #:
    #: [[LH-055]]'s port moves the estate privileges onto `type estate`, and the single setting was
    #: answering two unrelated questions. This one names the warehouse the estate's own root serves: its id
    #: is the first segment of every name on that root ([[LH-593]], :attr:`estate_warehouse`), the parent
    #: of every namespace directly beneath it, and where `can_create_namespace` is asked for one. MEASURED
    #: on the live store 2026-09-22 it is load-bearing: `warehouse:lance_catalog` carries 7 `child` edges
    #: and 10 principal grants, which every write under it inherits through.
    #:
    #: THINGS BREAK IF THESE TWO EVER COLLAPSE BACK INTO ONE, each measured rather than argued:
    #: `namespace#parent` is typed `[warehouse, namespace]` and every `… from parent` rung it inherits
    #: must exist on the parent's type, so an `estate:` parent is a model error and a lost cascade;
    #: `can_create_namespace` is not an estate relation, so a create beneath the estate's own warehouse
    #: would be refused; and maintenance's reconcile excludes the platform's own warehouse from the ghost
    #: report by matching the root's TYPE, so a non-warehouse root reports the default warehouse as a
    #: ghost on every tick.
    fga_default_warehouse_object: str = Field(default="warehouse:lance_catalog", alias="RASK_FGA_DEFAULT_WAREHOUSE_OBJECT")

    @property
    def estate_warehouse(self) -> str:
        """The id of the estate's own warehouse: the first segment of every name served from the estate's own root."""
        return self.fga_default_warehouse_object.partition(":")[2]

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
        them from prefix-by-suffix precisely so no literal survives in the tree.)

        SCANS BOTH SOURCES pydantic-settings would read: the process environment AND the configured
        `env_file`. It checked only `os.environ` at first, justified by "every deployment path in this
        estate sets real env vars" — but settings classes DO set `env_file=".env"`
        (the shared `Settings` among them). A retired name in a dotenv therefore bound silently while the guard
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


def _require_a_persons_idp_id(setting: str, idp_id: str) -> None:
    """`ValueError` naming `setting` when `idp_id` is a machine's or outside the idp-id grammar."""
    if is_machine_idp(idp_id):
        raise ValueError(f"{setting} {idp_id!r}, a machine identity's idp id")
    try:
        require_idp_id(idp_id)
    except ValueError as exc:
        raise ValueError(f"{setting} an idp id no principal can carry: {exc}") from exc


#: The claims every verified token carries (`lakehouse.governed.oidc.IDToken`), so none of them marks a client's.
_CLAIMS_EVERY_TOKEN_CARRIES = frozenset({"iss", "sub", "aud", "exp", "iat"})


class ClientMarker(BaseModel):
    """The claim an issuer marks its client's own token with, and the value it must hold (any, when unset)."""

    model_config = ConfigDict(frozen=True)

    claim: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def _marks_something(self) -> Self:
        if self.claim in _CLAIMS_EVERY_TOKEN_CARRIES:
            raise ValueError(f"the client marker names {self.claim!r}, a claim every token carries, so it marks nothing")
        return self


class OidcProvider(BaseModel):
    """One issuer a person or a client signs in at, and the rule its tokens are named by (`lakehouse.governed.idp_identity`)."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(min_length=1)
    idp_id: str
    #: Split-horizon fetch: discovery and keys fetched here while tokens keep `issuer`.
    discovery_url: str | None = None
    subject_claims: tuple[str, ...] | None = None
    connectors: dict[str, str] = Field(default_factory=dict)
    client_marker: ClientMarker | None = None

    @model_validator(mode="after")
    def _one_rule(self) -> Self:
        """One rule names a person, under idp ids a person may carry."""
        if self.subject_claims is not None:
            if self.connectors:
                raise ValueError("subject claims are never read beside a Dex connector map, which reports its user id in federated_claims")
            if not self.subject_claims or not all(self.subject_claims):
                raise ValueError("the subject claims name no claim, or an empty one")
        if "" in self.connectors:
            raise ValueError("the connector map names an empty connector id")
        _require_a_persons_idp_id("the idp id is", self.idp_id)
        for connector, idp_id in self.connectors.items():
            _require_a_persons_idp_id(f"the connector map maps {connector!r} to", idp_id)
        return self

    @property
    def idp_ids(self) -> frozenset[str]:
        """Every idp id this issuer's people and clients are named under, which no other issuer may name."""
        return frozenset({self.idp_id, f"{self.idp_id}{CLIENT_IDP_SUFFIX}", *self.connectors.values()})


class OidcSettings:
    """The bearer-token verifier's knobs. Mix in alone only alongside `FgaSettings`."""

    oidc_enabled: bool = Field(default=False, alias="RASK_OIDC_ENABLED")
    oidc_issuer: str | None = Field(default=None, alias="RASK_OIDC_ISSUER")
    oidc_audience: str | None = Field(default=None, alias="RASK_OIDC_AUDIENCE")
    #: Split-horizon fetch (reverse-proxied IdP): pull discovery/JWKS in-cluster while tokens keep the
    #: PUBLIC issuer string. Unset = derive the fetch URL from the issuer.
    oidc_discovery_url: str | None = Field(default=None, alias="RASK_OIDC_DISCOVERY_URL")
    #: How long an issuer's discovery document and key set are served before they are fetched again, in the background
    #: (`lakehouse.governed.oidc`): a key the issuer drops stops verifying within this. A request never waits on it.
    oidc_cache_ttl: int = Field(default=300, gt=0, alias="RASK_OIDC_CACHE_TTL")
    oidc_leeway: int = Field(default=60, alias="RASK_OIDC_LEEWAY")
    oidc_allow_insecure: bool = Field(default=False, alias="RASK_OIDC_ALLOW_INSECURE")
    #: The idp id of the issuer's people and clients ([[XC-214]], `lakehouse.governed.idp_identity`), required with OIDC:
    #: `corp` makes the person the issuer names `s` the principal `corp~s`, and its client `etl` signed in for itself
    #: `corp-client~etl`. Under `oidc_connectors` it names the clients alone, and the connectors' idp ids the people.
    oidc_idp_id: str | None = Field(default=None, alias="RASK_OIDC_IDP_ID")
    #: The claims a person's subject is read from, the first the token carries winning; unset is `sub` alone, which
    #: OpenID Connect Core 1.0 § 2 makes unique and never reassigned within the issuer. A provider whose `sub` is not the
    #: stable id a grant needs names another first (Entra's `oid`). Never read beside `oidc_connectors`.
    oidc_subject_claims: list[str] | None = Field(default=None, alias="RASK_OIDC_SUBJECT_CLAIMS")
    #: The idp id each Dex connector's users are principals of, keyed by the connector id Dex reports in
    #: `federated_claims` ([[LH-063]], `lakehouse.governed.idp_identity`): `{"local": "dex"}` makes the user id
    #: `u` of connector `local` the principal `dex~u`. Renaming a connector adds its new id under the same idp id, so
    #: no grant is re-keyed. A connector absent here authenticates nobody. Empty unless the issuer is such a Dex.
    oidc_connectors: dict[str, str] = Field(default_factory=dict, alias="RASK_OIDC_CONNECTORS")
    #: How the issuer marks a token its client was issued for itself where its `sub` is not the client's id (Keycloak's
    #: service-account user, `{"claim": "client_id"}`; Entra's `{"claim": "idtyp", "value": "app"}`). Unset, a token is
    #: a client's when its `sub` is the `client_id` it names (RFC 9068 § 2.2).
    oidc_client_marker: ClientMarker | None = Field(default=None, alias="RASK_OIDC_CLIENT_MARKER")
    #: Further issuers a person or a client signs in at, beside `oidc_issuer` (which the console signs in at), each with
    #: its own idp id and naming rule. A token is named by the issuer its `iss` is, and every issuer's idp ids are its own.
    oidc_providers: list[OidcProvider] = Field(default_factory=list, alias="RASK_OIDC_PROVIDERS")
    #: AN ACKNOWLEDGEMENT, NOT A SWITCH — it turns nothing off. `oidc_enabled` already decides whether
    #: this service authenticates; this says an operator MEANT the answer to be "no". Without it,
    #: "auth off because I meant it" and "auth off because nothing set it" are indistinguishable, and
    #: only the second is a vulnerability. `assert_authentication_configured` refuses to boot on that
    #: ambiguity alone (Q17-6 / §F2-2).
    insecure_allow_unauthenticated: bool = Field(default=False, alias="RASK_INSECURE_ALLOW_UNAUTHENTICATED")
    #: The cluster's service-account issuer, the one way a service authenticates ([[LH-220]], ADR 0156 C5). A projected
    #: token is verified offline against its key set and is the subject `kubernetes~system:serviceaccount:<ns>:<sa>`,
    #: whatever its namespace; OpenFGA alone decides what that subject may do. Unset, this door admits no service.
    sa_issuer: str | None = Field(default=None, alias="RASK_SA_ISSUER")
    sa_audience: str | None = Field(default=None, alias="RASK_SA_AUDIENCE")
    #: The issuer serves discovery and its keys only to a bearer from a private CA (k3s, measured 2026-10-02).
    sa_fetch_token_file: str | None = Field(default=None, alias="RASK_SA_FETCH_TOKEN_FILE")
    sa_ca_file: str | None = Field(default=None, alias="RASK_SA_CA_FILE")
    #: The longest lifetime (`exp - iat`) a service-account token may have been minted with ([[LH-585]]). The issuer
    #: signs whatever lifetime a TokenRequest asks for, and a token verified offline stays good after its pod and its
    #: account are deleted, so this bounds how long. An hour is a TokenRequest's own default; a pod's projected token
    #: is 600 s.
    sa_max_token_seconds: int = Field(default=3600, gt=0, alias="RASK_SA_MAX_TOKEN_SECONDS")

    @model_validator(mode="after")
    def _validate_service_accounts(self) -> Self:
        """An issuer with no audience would accept a token minted for any other door, or refuse every one."""
        if self.sa_issuer and not self.sa_audience:
            raise ValueError("RASK_SA_AUDIENCE is required with RASK_SA_ISSUER")
        if self.sa_issuer and not self.sa_issuer.startswith("https://"):
            raise ValueError("RASK_SA_ISSUER must use HTTPS")
        # A door routes a bearer by its unverified `iss`, the service-account issuer first, so one issuer named twice
        # would answer every person's token as a service account's. Two providers claiming one issuer are refused at
        # boot instead.
        if self.sa_issuer and any(provider.issuer.rstrip("/") == self.sa_issuer.rstrip("/") for provider in self.oidc_person_providers):
            raise ValueError("RASK_SA_ISSUER and a person's issuer name one issuer: its tokens would be routed as service accounts' and never as a person's")
        return self

    @property
    def oidc_person_providers(self) -> list[OidcProvider]:
        """Every issuer a person or a client signs in at: `oidc_issuer`, then `oidc_providers`. Empty with OIDC off."""
        if not (self.oidc_enabled and self.oidc_issuer and self.oidc_idp_id):
            return []
        try:
            primary = OidcProvider(
                issuer=self.oidc_issuer,
                idp_id=self.oidc_idp_id,
                discovery_url=self.oidc_discovery_url,
                subject_claims=None if self.oidc_subject_claims is None else tuple(self.oidc_subject_claims),
                connectors=self.oidc_connectors,
                client_marker=self.oidc_client_marker,
            )
        except ValidationError as exc:
            refusals = "; ".join(str(error["msg"]).removeprefix("Value error, ") for error in exc.errors())
            raise ValueError(f"RASK_OIDC_ISSUER's settings are refused: {refusals}") from None
        return [primary, *self.oidc_providers]

    @model_validator(mode="after")
    def _validate_person_naming(self) -> Self:
        """Each issuer names its people under idp ids of its own, never a machine's (`kubernetes`, `<idp id>-client`)."""
        if not self.oidc_enabled:
            return self
        if not self.oidc_idp_id:
            raise ValueError("RASK_OIDC_IDP_ID is required when OIDC is enabled: it names the issuer's people, or under RASK_OIDC_CONNECTORS its clients")
        claimed: dict[str, str] = {}
        issuers: set[str] = set()
        for provider in self.oidc_person_providers:
            issuer = provider.issuer.rstrip("/")
            if issuer in issuers:
                raise ValueError(f"{issuer} is named as an issuer twice: one token would be named by two rules")
            issuers.add(issuer)
            for idp_id in provider.idp_ids:
                if claimed.setdefault(idp_id, issuer) != issuer:
                    raise ValueError(f"the idp id {idp_id!r} is named by {claimed[idp_id]} and {issuer}: their people would share one namespace")
            if not self.oidc_allow_insecure and not issuer.startswith("https://"):
                raise ValueError(f"the issuer {issuer} must use HTTPS (set RASK_OIDC_ALLOW_INSECURE=true for a dev IdP)")
        return self

    @model_validator(mode="after")
    def _validate_oidc(self) -> Self:
        """Fail fast at construction rather than fail open — or fail confusingly — at request time."""
        if self.oidc_enabled and not (self.oidc_issuer and self.oidc_audience):
            raise ValueError("RASK_OIDC_ISSUER and RASK_OIDC_AUDIENCE are required when OIDC is enabled")
        # AN `http://` ISSUER IS A MISCONFIGURATION, AND IT BELONGS HERE. `_require_https` catches it
        # only at VERIFY time, refusing every bearer — valid or not — with a 503 on the first request
        # after a healthy-looking startup. A scheme is knowable at construction, which is what this
        # validator is for (`_validate_person_naming` holds every issuer to it).
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
    "which subject" from config, so it composes the two mixins directly instead, or takes
    `FgaSettings` alone (maintenance).
    """

    @model_validator(mode="after")
    def _authorization_requires_authentication(self) -> Self:
        if self.fga_enabled and not self.oidc_enabled:
            raise ValueError("RASK_OIDC_ENABLED is required when RASK_FGA_ENABLED is set (authz needs a verified subject)")
        return self


def assert_authentication_configured(*, oidc_enabled: bool, insecure_allow_unauthenticated: bool) -> None:
    """Fail closed at startup: a service that authenticates nobody must SAY SO.

    `oidc_enabled` defaults to False and `authenticate()` returns `None` when it is off, so every
    route opens. The chart flips it on, which leaves the DEPLOYED estate closed and the CODE open —
    and the gap is any service run another way: a `uv run`, a compose stack, an image started outside
    the chart. So a service started without an authenticator refuses to boot unless it is told that is meant.

    THE REFUSAL IS ON THE AMBIGUITY, NOT ON BEING OPEN. Flipping the default would change what an
    explicitly-configured deployment does and would red every suite that constructs settings without
    naming auth. What is actually indefensible is that "off because I meant it" and "off because
    nothing set it" look identical, so an operator resolves it by saying which they meant. No-op once
    either is true.
    """
    if oidc_enabled or insecure_allow_unauthenticated:
        return
    raise RuntimeError(
        "this service would start with NO authentication: RASK_OIDC_ENABLED is off and nothing "
        "acknowledged it, so every route would serve any caller that can reach the port. Set "
        "RASK_OIDC_ENABLED=true (with RASK_OIDC_ISSUER + RASK_OIDC_AUDIENCE), or set "
        "RASK_INSECURE_ALLOW_UNAUTHENTICATED=true to declare that an open service is what you meant."
    )
