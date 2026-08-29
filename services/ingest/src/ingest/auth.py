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

* the Dapr app-api-token — service-to-service, and project-BLIND. The shared token authenticates a
  service, not a tenant, so it may only ingest into the CONFIGURED project. Honouring an arbitrary
  requested project would let any token holder write into every tenant, which is the escalation the
  per-project check exists to prevent.
* a signed-in OIDC principal holding `can_administer` on `project:{requested}` — the human door, and
  the only one that may cross tenants. The check targets the project the request NAMES, not a fixed
  one: authorization scope must equal write scope, or an admin of project A passes the gate while the
  rows land in project B.

**Fail-closed at every step.** No service token configured is dev-open, matching the estate's other
doors. OIDC enabled with no verifier wired is 503 — an infrastructure fault, not a caller verdict, so
a valid admin bearer is never misreported as denied. An FGA outage is 503, never a silent allow. A
request carrying neither credential is 403.

Every decision is audited on `lance.audit`: an ingest is exactly the operation the admin audit viewer
reviews, and a door whose allows are invisible cannot be reviewed at all.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import Depends, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.oidc import verify_off_loop
from service_kit.governed.settings import GovernedAuthSettings


if TYPE_CHECKING:
    from collections.abc import Iterable

    from openfga_sdk import OpenFgaClient

    from service_kit.governed.oidc import OIDCVerifier


class IngestAuthSettings(GovernedAuthSettings, BaseSettings):
    """The auth half of the ingest service's config.

    A separate model rather than fields on the fleet `Settings`, because `GovernedAuthSettings` is the
    estate's shared vocabulary (`LANCE_OIDC_*`, `LANCE_FGA_*`) and re-spelling those names under a
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

    #: The project the SERVICE token may ingest into. The shared token carries no tenant identity, so
    #: it is pinned here; crossing tenants requires a user bearer and its per-project FGA check.
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
        raise PermissionDeniedError("ingest needs project admin (can_administer) or the service token")


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
    #: The audit subject: `service:<app-id>` for the service door, the token's `sub` for a human.
    subject: str | None = None
    refusal: str | None = None
    #: Set when the refusal is worth an audit line — a VALID service token refused because it arrived
    #: through a public front door looks like a bug until the audit says why.
    refusal_reason: str | None = None


async def _resolve_caller(
    request: Request,
    settings: IngestAuthSettings,
    dapr_api_token: str | None,
    authorization: str | None,
    dapr_caller_app_id: str | None,
) -> _Caller:
    """Answer "who is this" once. Raises only for faults that are true of the CALL, never of a row.

    `ServiceUnavailableError` (authentication enabled but unwired) and `UnauthenticatedError` (a
    malformed or invalid bearer) propagate, because neither can be true of one project and false of
    the next — the listing's own docstring is explicit that rendering them as an empty page tells a
    caller their token works and they own nothing.
    """
    expected = os.environ.get("APP_API_TOKEN")
    # An absent service token means "this deployment has no SERVICE door" — never "this deployment has
    # no door". Returning on it alone was a full bypass of the user path as well: an estate with
    # `LANCE_OIDC_ENABLED=true`, a live FGA client and a blank or unset `APP_API_TOKEN` accepted every
    # ingest from anyone who could reach the port, while every surface reported authorization as ON.
    # That is open_python-audit's ING-01, and the blank case is the likely one — a secret that renders
    # empty is far more common than one nobody wired, and it fails OPEN rather than loudly.
    #
    # So the open posture is now conditioned on what it always claimed to mean: no service token AND no
    # user authentication configured, i.e. the documented local-dev stack. With either configured, the
    # request falls through to the OIDC + FGA project-admin path and, failing that, to the
    # `PermissionDeniedError` the callers below raise.
    if not expected and not (settings.oidc_enabled or settings.fga_enabled):
        return _Caller(mode="open")  # dev: nothing configured to authenticate against, exactly like the estate's other doors

    # The caller is the PUBLIC front door, so the token daprd stamped on the way in says nothing about
    # who is actually asking. Fall through to the user-bearer path — never allow on the token alone.
    from_public_door = is_public_caller(dapr_caller_app_id)

    # `expected` is reachable as None (auth on, no service token), so it is tested FIRST — the
    # comparison would otherwise raise AttributeError on `None.encode()` and answer 500 where the
    # honest answer is "this deployment has no service door, use a bearer".
    if expected and not from_public_door and dapr_api_token and secrets.compare_digest(dapr_api_token.encode(), expected.encode()):
        # The caller is recorded, not just the fact that A service called: an audit line that says
        # only "service" cannot answer "which one", which is the question an incident starts from.
        return _Caller(mode="service", subject=f"service:{dapr_caller_app_id or 'direct'}")

    if from_public_door and dapr_api_token and not authorization:
        # Named explicitly so the denial is legible: the request DID carry a valid service token and
        # was refused anyway, which looks like a bug until you know why.
        return _Caller(
            mode="refused",
            subject=f"service:{dapr_caller_app_id}",
            refusal=(f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller — sign in and retry"),
            refusal_reason="public_caller",
        )

    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if settings.oidc_enabled and verifier is None and authorization:
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None and authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise UnauthenticatedError("malformed bearer")
        try:
            # Off the loop: verify() does synchronous OIDC discovery + JWKS fetches (up to 15s) on a
            # cold cache or key rotation — inline it stalled every in-flight request in the pod,
            # probes included (open_python-audit ING-02). Same rule _DaprWorkflowStarter.start
            # already states one module over.
            #
            # The hop itself moved into `service_kit.governed.oidc` rather than living here: this fix
            # was written on THIS door and never reached the medallion door, which is a copy of this
            # function and went on blocking the cascade head. One seam, so a fourth door inherits it.
            token = await verify_off_loop(verifier, raw)
        except UnauthenticatedError:
            raise UnauthenticatedError("invalid token") from None
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
    dapr_api_token: str | None = None,
    authorization: str | None = None,
    dapr_caller_app_id: str | None = None,
) -> str | None:
    """Allow EITHER the Dapr app-api-token (service, configured project only) OR a project admin.

    Plain parameters, NOT FastAPI bindings: every call site invokes this positionally with values the
    ROUTE already extracted (each governed route declares its own `Header()`-bound params and passes
    them in). Header()/Depends() here would be inert, and wiring this in as an actual dependency would
    bind `project` as a query param defaulting to None — silently scoping the admin check to the
    configured project instead of the body's, a cross-project regression. So the signature stays plain.

    `project` is the project the REQUEST names — the routes pass `body.project` / the run's recorded
    project — so the admin check always targets what the caller is actually writing into.

    `dapr_caller_app_id` is the invoking Dapr app-id. It is what separates "a service called me" from
    "the public front door called me on behalf of someone anonymous". The list of front doors is the
    ESTATE's, in `service_kit.governed.dapr_auth` — a per-service copy would let a newly added edge be
    refused by one door and trusted by another.

    The single-project door. `authorize_ingest_projects` is its many-project twin and they share
    `_resolve_caller`, so the two cannot answer the same credential differently.
    """
    caller = await _resolve_caller(request, settings, dapr_api_token, authorization, dapr_caller_app_id)
    obj = f"project:{project or settings.service_project}"

    if caller.mode == "open":
        return None

    if caller.mode == "service":
        if project and project != settings.service_project:
            audit("ingest_service_token", DENY, subject=caller.subject, resource=obj, reason="cross_project")
            raise PermissionDeniedError("the service token cannot ingest into another project; use a project-admin bearer")
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
        # every decision above is unchanged. A service-token call returns None: no human is behind it.
        return caller.subject

    if caller.refusal_reason is not None:
        audit("ingest_service_token", DENY, subject=caller.subject, resource=obj, reason=caller.refusal_reason)
    raise PermissionDeniedError(caller.refusal or "invalid or missing ingest credential")


async def authorize_ingest_projects(
    request: Request,
    settings: IngestAuthSettings,
    projects: Iterable[str],
    dapr_api_token: str | None = None,
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

    caller = await _resolve_caller(request, settings, dapr_api_token, authorization, dapr_caller_app_id)

    if caller.mode == "open":
        return frozenset(unique)

    if caller.mode == "service":
        # Project-BLIND by construction: the shared token authenticates a service, not a tenant, so it
        # sees exactly the one project it may write into — the same rule the single door enforces with
        # its cross-project refusal.
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
