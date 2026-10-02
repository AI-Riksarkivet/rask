"""The door on the ingest control API — dual-auth, fail-closed.

`POST /v1/ingests` shipped with NO authentication at all. That is worse than an ordinary missing
gate, because the endpoint takes a caller-supplied source: with `local-dir` it was one unauthenticated
request to read the ingest pod's own filesystem into a governed table, and with `s3-prefix` it is an
unauthenticated writer into any project's bronze tier. The path confinement in `adapters.py` removed
the file-read primitive; this removes the open door.

Deliberately the SAME shape as `medallion/api/produce_auth.py::authorize_produce`, because it is the
same question — "may this caller drive a write into this project's tiers?" — and two different answers
to one question is how an estate ends up with a weak door and a strong one. The differences are named
where they exist, not invented.

**Two doors, both closed by default:**

* a service's projected service-account token ([[LH-220]], D1), verified against the cluster issuer and mapped to
  the subject its account names (`app.state.sa_oidc`). The service caller is held to the CONFIGURED project:
  honouring an arbitrary requested project would let one service write into every tenant, which is the escalation
  the per-project check exists to prevent. A bearer the cluster issuer claims is answered by that verifier alone and
  never falls through to the human door.
* a signed-in OIDC principal holding `can_administer` on `project:{requested}` — the human door, and
  the only one that may cross tenants. The check targets the project the request NAMES, not a fixed
  one: authorization scope must equal write scope, or an admin of project A passes the gate while the
  rows land in project B.

**Fail-closed at every step.** Nothing configured to authenticate against (no service-account issuer, no OIDC, no
FGA) is dev-open, matching the estate's other doors. OIDC enabled with no verifier wired is 503 — an infrastructure fault, not a caller verdict, so
a valid admin bearer is never misreported as denied. An FGA outage is 503, never a silent allow. A
request carrying neither credential is 403.

Every decision is audited on `lance.audit`: an ingest is exactly the operation the admin audit viewer
reviews, and a door whose allows are invisible cannot be reviewed at all.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Literal

import jwt
from fastapi import Depends, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.oidc import ProviderUnavailableError, verify_off_loop
from service_kit.governed.settings import GovernedAuthSettings


if TYPE_CHECKING:
    from collections.abc import Iterable

    from openfga_sdk import OpenFgaClient

    from service_kit.governed.machine_identity import ServiceAccountVerifier
    from service_kit.governed.oidc import OIDCVerifier


class IngestAuthSettings(GovernedAuthSettings, BaseSettings):
    """The auth half of the ingest service's config.

    A separate model rather than fields on the fleet `Settings`, because `GovernedAuthSettings` is the
    estate's shared vocabulary (`RASK_OIDC_*`, `RASK_FGA_*`) and re-spelling those names under a
    `RASK_INGEST_` prefix would give this one service its own dialect for settings every other
    governed service already reads.
    """

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT, RAY_DASHBOARD_URL) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True, env_prefix="LANCE_")

    #: The project a SERVICE caller may ingest into; crossing tenants requires a user bearer and its per-project FGA
    #: check.
    #:
    #: `RASK_INGEST_SERVICE_PROJECT` is the deployment knob and takes precedence; the model reads it
    #: directly rather than through a post-construct `os.environ` patch, so caching `get_auth_settings`
    #: cannot strand a stale value.
    service_project: str = Field(default="demo", validation_alias=AliasChoices("RASK_INGEST_SERVICE_PROJECT", "LANCE_SERVICE_PROJECT"))


@lru_cache
def get_auth_settings() -> IngestAuthSettings:
    """The auth settings, built ONCE. `AuthSettingsDep` resolves this per request on every governed
    route, and an uncached `BaseSettings` re-reads `.env` from disk each time. `cache_clear` is the
    hook tests use when they mutate the environment between constructions."""
    return IngestAuthSettings()


AuthSettingsDep = Annotated[IngestAuthSettings, Depends(get_auth_settings)]


async def _require_admin(client: OpenFgaClient, *, user: str, obj: str) -> None:
    """`can_administer` on the project, audited, 403 on denial and 503 on outage.

    The outage case is separated on purpose: reporting an unreachable OpenFGA as a denial tells the
    caller they lack a permission they may well hold, and hides an incident behind a 403 that nobody
    pages on.
    """
    try:
        allowed = await fga.check(client, user=user, relation="can_administer", obj=obj)
    except ServiceUnavailableError:
        audit("can_administer", FAILURE, subject=user, resource=obj, reason="authz_unavailable")
        raise ServiceUnavailableError("authorization service is not available") from None
    audit("can_administer", ALLOW if allowed else DENY, subject=user, resource=obj)
    if not allowed:
        raise PermissionDeniedError("ingest needs project admin (can_administer) or a service caller")


class _Caller(BaseModel):
    """WHO is asking, resolved once — the authentication half, before any project is named.

    Authentication and authorization used to be one straight-line function, and that was fine while
    every door named exactly one project. `GET /ingests` does not: the listing spans tenants, so the
    straight-line version re-ran the WHOLE door per row — up to 200 JWT verifications and 200
    OpenFGA `check`s for one request (ING-05). Splitting at the seam where the caller stops mattering
    and the project starts is what lets the listing verify once and ask OpenFGA once.

    `refusal` carries the message the single-project door raises, so the two paths cannot drift into
    two different explanations of the same denial. `mode="refused"` is not an error here because the
    LISTING must not raise on it: a 403 for the whole call would leak that runs exist.
    """

    model_config = {"frozen": True}

    mode: Literal["open", "service", "user", "refused"]
    #: The audit subject: the subject a service's account maps to, or the token's `sub` for a human.
    subject: str | None = None
    refusal: str | None = None
    #: Set when the refusal is worth an audit line — a VALID service caller refused because it arrived
    #: through a public front door looks like a bug until the audit says why.
    refusal_reason: str | None = None


def _service_account_issued(raw: str, verifier: ServiceAccountVerifier | None, issuer: str | None) -> bool:
    """Whether the bearer claims the cluster's service-account issuer: the routing question, asked unverified.

    Answered from the configured issuer when the verifier did not build, so such a token still routes to the service
    branch and is refused 503 there, never handed to the human door's verifier.
    """
    if verifier is not None:
        return verifier.issued(raw)
    if not issuer:
        return False
    try:
        claimed = jwt.decode(raw, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == issuer.rstrip("/")


async def _resolve_caller(
    request: Request,
    settings: IngestAuthSettings,
    authorization: str | None,
    dapr_caller_app_id: str | None,
) -> _Caller:
    """Answer "who is this" once. Raises only for faults that are true of the CALL, never of a row.

    `ServiceUnavailableError` (authentication enabled but unwired, or its issuer unusable) and
    `UnauthenticatedError` (a malformed or invalid bearer) propagate, because neither can be true of one project and false of
    the next — the listing's own docstring is explicit that rendering them as an empty page tells a
    caller their token works and they own nothing.
    """
    # Open only when NOTHING is configured to authenticate against, the documented local-dev stack. An absent service
    # door is never an absent door (docs/DECISIONS.md "The Python estate audit", ING-01): with OIDC or FGA on, a
    # request no verifier answers falls through to the `PermissionDeniedError` the callers below raise.
    if not (settings.sa_issuer or settings.oidc_enabled or settings.fga_enabled):
        return _Caller(mode="open")  # dev: nothing configured to authenticate against, exactly like the estate's other doors

    scheme, _, raw = (authorization or "").partition(" ")
    bearer = raw if scheme.lower() == "bearer" and raw else None

    sa_verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if bearer is not None and _service_account_issued(bearer, sa_verifier, settings.sa_issuer):
        if sa_verifier is None:
            audit("authn", FAILURE, reason="verifier_unavailable")
            raise ServiceUnavailableError("service authentication is enabled but unavailable")
        try:
            # Off the loop: a cold key set or a rotated key fetches discovery and JWKS synchronously (ING-02).
            principal = await asyncio.to_thread(sa_verifier.verify, bearer)
        except UnauthenticatedError:
            audit("authn", FAILURE, reason="invalid_token")
            raise UnauthenticatedError("invalid token") from None
        except ProviderUnavailableError as exc:
            audit("authn", FAILURE, reason="verifier_unavailable")
            raise ServiceUnavailableError("service authentication is enabled but unavailable") from exc
        if is_public_caller(dapr_caller_app_id):
            # A machine credential relayed by a PUBLIC front door names the proxy's hop, not a caller this door may
            # trust as a service; the estate's list of front doors lives in `service_kit.governed.dapr_auth`.
            return _Caller(
                mode="refused",
                subject=principal.sub,
                refusal=f"{dapr_caller_app_id!r} is a public front door: a service credential does not pass through it — sign in and retry",
                refusal_reason="public_caller",
            )
        return _Caller(mode="service", subject=principal.sub)

    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if settings.oidc_enabled and verifier is None and authorization:
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None and authorization:
        if bearer is None:
            raise UnauthenticatedError("malformed bearer")
        try:
            # Off the loop: verify() does synchronous OIDC discovery + JWKS fetches (up to 15s) on a
            # cold cache or key rotation — inline it stalled every in-flight request in the pod,
            # probes included (docs/DECISIONS.md "The Python estate audit" ING-02). The hop lives in
            # `service_kit.governed.oidc` so every door inherits it.
            token = await verify_off_loop(verifier, bearer)
        except UnauthenticatedError:
            raise UnauthenticatedError("invalid token") from None
        except ProviderUnavailableError as exc:
            # The unwired-verifier branch's fact arriving later, so its body: the spec's `code` 17, not the fleet's.
            raise ServiceUnavailableError("authentication is enabled but unavailable") from exc
        return _Caller(mode="user", subject=token.sub)

    return _Caller(mode="refused", refusal="invalid or missing ingest credential")


def _fga_client(request: Request) -> OpenFgaClient:
    """The store this door decides against, or 503. OIDC on with FGA unwired is never an allow."""
    client: OpenFgaClient | None = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    return client


async def authorize_ingest(
    request: Request,
    settings: IngestAuthSettings,
    project: str | None = None,
    authorization: str | None = None,
    dapr_caller_app_id: str | None = None,
) -> str | None:
    """Allow EITHER a service caller (configured project only) OR a project admin.

    Plain parameters, NOT FastAPI bindings: every call site invokes this positionally with values the
    ROUTE already extracted (each governed route declares its own `Header()`-bound params and passes
    them in). Header()/Depends() here would be inert, and wiring this in as an actual dependency would
    bind `project` as a query param defaulting to None — silently scoping the admin check to the
    configured project instead of the body's, a cross-project regression. So the signature stays plain.

    `project` is the project the REQUEST names — the routes pass `body.project` / the run's recorded
    project — so the admin check always targets what the caller is actually writing into.

    `dapr_caller_app_id` is the invoking Dapr app-id. It is what separates "a service called me" from
    "the public front door relayed a machine credential". The list of front doors is the
    ESTATE's, in `service_kit.governed.dapr_auth` — a per-service copy would let a newly added edge be
    refused by one door and trusted by another.

    The single-project door. `authorize_ingest_projects` is its many-project twin and they share
    `_resolve_caller`, so the two cannot answer the same credential differently.
    """
    caller = await _resolve_caller(request, settings, authorization, dapr_caller_app_id)
    obj = f"project:{project or settings.service_project}"

    if caller.mode == "open":
        return None

    if caller.mode == "service":
        if project and project != settings.service_project:
            audit("ingest_service_token", DENY, subject=caller.subject, resource=obj, reason="cross_project")
            raise PermissionDeniedError("a service caller cannot ingest into another project; use a project-admin bearer")
        audit("ingest_service_token", ALLOW, subject=caller.subject, resource=obj)
        return None

    # `and caller.subject` narrows AND fails closed: a bearer whose `sub` is blank is not an identity,
    # and falling through to the refusal below is the only safe reading of one.
    if caller.mode == "user" and caller.subject:
        await _require_admin(_fga_client(request), user=caller.subject, obj=obj)
        # RETURNED, not discarded. This door is the LAST place the human exists: everything downstream
        # is a workflow activity running behind this service's own token, and lineage's `enforce_author`
        # stamps THAT as the author — so an ingest run used to be announced to an inbox named
        # `service-ingest`. The value is a TARGETING hint only (it rides `lance.originator`, which the
        # notifications plane re-authorizes per recipient at delivery); it widens no authorization, and
        # every decision above is unchanged. A service call returns None: no human is behind it.
        return caller.subject

    if caller.refusal_reason is not None:
        audit("ingest_service_token", DENY, subject=caller.subject, resource=obj, reason=caller.refusal_reason)
    raise PermissionDeniedError(caller.refusal or "invalid or missing ingest credential")


async def authorize_ingest_projects(
    request: Request,
    settings: IngestAuthSettings,
    projects: Iterable[str],
    authorization: str | None = None,
    dapr_caller_app_id: str | None = None,
) -> frozenset[str]:
    """WHICH of `projects` this caller may see — one authentication, one OpenFGA round trip.

    The filtering twin of `authorize_ingest`, for the cross-tenant listing. Calling the single door
    per row gave the same body and cost a JWT verification plus a `check` per record; `authz.md`'s
    rule is "prefer `batch_check` over many `check`s when filtering", which is how `services/viewer`
    already lists.

    RETURNS rather than raises for a per-row verdict: a caller who may see none of these runs gets an
    empty page, because a 403 for the whole call would leak that runs exist. The two faults that are
    true of the CALL — an unwired/unreachable authorization service (503) and an invalid bearer
    (401) — still propagate, so an outage never renders as "you own nothing".
    """
    unique = sorted(set(projects))
    if not unique:
        return frozenset()

    caller = await _resolve_caller(request, settings, authorization, dapr_caller_app_id)

    if caller.mode == "open":
        return frozenset(unique)

    if caller.mode == "service":
        # A service caller sees exactly the one project it may write into — the same rule the single door
        # enforces with its cross-project refusal.
        allowed = frozenset(p for p in unique if p == settings.service_project)
        for project in unique:
            audit("ingest_service_token", ALLOW if project in allowed else DENY, subject=caller.subject, resource=f"project:{project}")
        return allowed

    # See the twin above: a blank `sub` is not an identity, and falls through to an empty page.
    if caller.mode == "user" and caller.subject:
        client = _fga_client(request)
        objects = [f"project:{p}" for p in unique]
        try:
            verdicts = await fga.batch_check(client, user=caller.subject, relation="can_administer", objects=objects)
        except ServiceUnavailableError:
            audit("can_administer", FAILURE, subject=caller.subject, resource=",".join(objects), reason="authz_unavailable")
            raise ServiceUnavailableError("authorization service is not available") from None
        for obj in objects:
            audit("can_administer", ALLOW if verdicts.get(obj) else DENY, subject=caller.subject, resource=obj)
        return frozenset(p for p in unique if verdicts.get(f"project:{p}"))

    if caller.refusal_reason is not None:
        for project in unique:
            audit("ingest_service_token", DENY, subject=caller.subject, resource=f"project:{project}", reason=caller.refusal_reason)
    return frozenset()
