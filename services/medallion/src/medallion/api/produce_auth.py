"""Dual-auth for ``POST /produce`` (#64): the DAPR app-api-token OR a signed-in project admin.

The cascade head must never be forgeable (a bronze-write event fabricates provenance), so the existing
service-to-service guard — the shared app-api-token — is kept UNCHANGED. This adds a SECOND, human door:
a signed-in OIDC user who holds ``can_administer`` on the project may trigger produce, so the web BFF can
forward the *user's* bearer and the web pod never holds the service token (no secrets-posture change).

WHICH PROJECT THE ADMIN CHECK NAMES depends on the door (owner ruling 2026-09-25, "Authorize on the
resource"). A door whose ``?project=`` is its WRITE TARGET checks that project (`authorize_produce`). A door
acting on an EXISTING resource authenticates only (`admit_caller`) and checks the project the resource
records (`require_project_admin`, `administered_projects`), so no caller-chosen value can move its gate.
A read of DEPLOYMENT config, the same for every tenant, admits any authenticated caller and checks no
project (`admit_config_read`).

Fail-closed at every step: no service token configured is a refusal unless the unauthenticated hatch is set;
a matching Dapr token passes (service path) — on a write-target door only into the CONFIGURED project,
since the shared token carries no tenant identity (crossing tenants takes a user bearer); otherwise an
OIDC bearer is REQUIRED and must be valid (else 401) AND resolve to a project admin (else 403), with an
OpenFGA outage failing to 503 — never a silent allow; a request carrying neither credential is 403. OIDC
enabled with NO verifier wired (startup/discovery skew) is 503 for a bearer-presenting caller — an
auth-layer outage, not a caller verdict (the catalog/lineage ``security.py`` invariant). Every door
decision — the ``can_administer`` allow/deny/outage AND the service-token acceptance — is audited on the
``lance.audit`` stream (#41), naming the project the call acts on, or the path a config read reads.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Annotated, Protocol

from fastapi import Depends, Header, Query, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.core.config import MedallionSettings
from service_kit.governed import dapr_auth, fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, SUCCESS, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.oidc import OIDCVerifier, ProviderUnavailableError, verify_off_loop
from service_kit.lakehouse.warehouse_registry import PROJECT_PATTERN


#: The optional per-tenant project (#84) — shared by the auth gate and the /produce route (FastAPI
#: deduplicates the identically-declared query param). Pattern-bound so an unsafe id 422s at the edge.
#:
#: The bounds MATCH the pattern rather than sitting outside it. ``PROJECT_PATTERN`` is the catalog's
#: mint rule, which fixes the length at 3-63; a schema advertising 1-64 documents a project id no
#: control plane can issue and the pattern then refuses, so a generated client's own validation and
#: this door's disagree about which ids are worth sending.
ProjectParam = Annotated[str | None, Query(min_length=3, max_length=63, pattern=PROJECT_PATTERN)]


async def _require_admin(fga_client: OpenFgaClient, *, user: str, obj: str) -> None:
    """Check ``can_administer`` on ``obj``, audit the decision, and raise 403 on denial / 503 on outage.

    Mirrors the catalog's ``fga_deps._require`` (#41 audit every authz decision): the cascade-head trigger
    is exactly the operation the admin audit viewer (#77) reviews, so its allow/deny/outage outcomes must
    land on the ``lance.audit`` stream like every other ``can_administer`` decision in the estate.
    """
    try:
        allowed = await fga.check(fga_client, user=user, relation="can_administer", obj=obj)
    except ServiceUnavailableError:  # authz layer down during a trigger attempt — audit, then fail closed
        audit("can_administer", FAILURE, subject=user, resource=obj, reason="authz_unavailable")
        raise ServiceUnavailableError("authorization service is not available") from None
    audit("can_administer", ALLOW if allowed else DENY, subject=user, resource=obj)
    if not allowed:
        raise PermissionDeniedError(f"{user} lacks can_administer on {obj}")


class ProducerCaller(BaseModel):
    """Who passed the producer's door, before any resource is read.

    ``subject`` is the verified person, still to be authorized on a project. ``service`` is the service
    the shared token admitted (``service:<app-id>``): decided whole at the door, and audited once the
    route has read which project it acts on. Neither set is the unconfigured-door hatch.
    """

    model_config = ConfigDict(frozen=True)

    subject: str | None = None
    service: str | None = None


class _HasAppApiToken(Protocol):
    """The one field `_expected_app_token` reads.

    A Protocol rather than `MedallionSettings` so the resolver is drivable without constructing the
    whole settings surface — the rule under test is WHICH SOURCE ANSWERS FIRST, and a test that had to
    build every unrelated field to ask that would be asserting through noise.
    """

    @property
    def app_api_token(self) -> str: ...


def _expected_app_token(settings: _HasAppApiToken) -> str:
    """The app token THIS DOOR verifies, resolved the way every other inbound door resolves it.

    ONE SECRET, ONE ACCESSOR. `core/config.py::outbound_app_token` exists because the outbound
    credential read `settings.app_api_token` directly and went silent when the estate's secrets rule
    moved the token off the environment; its docstring names `dapr_auth.expected_app_token` as "the
    single resolver the inbound doors use". This door did not use it. Measured on the deployed producer
    2026-09-19: `expected_app_token()` returns a token while `settings.app_api_token` is `''`, so the
    gate read "unconfigured" on an estate that has one and took its dev-open path — `GET /stage-runners`
    answered 200 with real data to a caller holding no credential at all.

    The typed setting stays as the FALLBACK so a deployment still carrying the env value keeps working;
    what changes is which source answers FIRST.

    It does not catch `SecretStoreUnreadable`, for `outbound_app_token`'s reason: a store outage must
    not degrade into an open door, which is the same argument one direction further in.
    """
    return dapr_auth.expected_app_token() or settings.app_api_token or ""


async def _admit(
    request: Request,
    settings: MedallionSettings,
    *,
    dapr_api_token: str | None,
    authorization: str | None,
    dapr_caller_app_id: str | None,
    resource: str,
) -> ProducerCaller:
    """AUTHENTICATE a producer caller: the service token is recognised here, a person only verified.

    Neither is authorized yet. The caller of this function does that on the write target
    (`authorize_produce`) or on the resource (`require_project_admin` / `administered_projects`), which
    is also where the service token's acceptance is audited, against the project it acts on.
    ``resource`` names what this door's own refusal is recorded against.
    """
    expected = _expected_app_token(settings)
    # UNCONFIGURED IS A REFUSAL, through the same function every sibling door calls. This door used to
    # open here, and the asymmetry was unreachable from either side: `require_dapr_token` refuses, this
    # admitted, and nothing said which was intended — on the CASCADE HEAD. Owner ruling 2026-09-19
    # (`docs/DECISIONS.md`); `RASK_ALLOW_UNAUTHENTICATED_DAPR` is what a deployment that means to run
    # open now says out loud. When the hatch is taken the caller is admitted with no subject, because
    # no verified subject exists on that path, never a guess.
    if not expected:
        dapr_auth.refuse_unconfigured_door(caller=dapr_caller_app_id)
        return ProducerCaller()
    # Service-to-service path: a matching Dapr app-api-token. The shared token names no principal, so
    # its audit subject is `service:<caller app-id>`.
    # THE MEASURED BYPASS. The gateway forwards through Dapr service invocation and the callee's
    # daprd stamps a valid `dapr-api-token` on the way in, so an ANONYMOUS public request reaches this
    # line already holding the estate's service credential. Measured on the sibling ingest door: 403
    # straight to the pod, 202 through the gateway. `/produce` writes bronze$events, fabricates
    # OpenLineage provenance and fires the whole bronze->silver->gold cascade; `/train` spends GPU.
    # A public caller therefore gets NO service-token path — it falls through to the bearer below.
    from_public_door = is_public_caller(dapr_caller_app_id)
    if from_public_door and dapr_api_token and not authorization:
        audit("produce_service_token", DENY, subject=f"service:{dapr_caller_app_id}", resource=resource, reason="public_caller")
        raise PermissionDeniedError(
            f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller — sign in and retry"
        )
    if not from_public_door and dapr_api_token and secrets.compare_digest(dapr_api_token.encode(), expected.encode()):
        return ProducerCaller(service=f"service:{dapr_caller_app_id or 'direct'}")
    # Human path: a signed-in person. Only when OIDC is configured + a verifier is wired.
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if settings.oidc_enabled and verifier is None and authorization:
        # OIDC enabled but no verifier wired (startup/discovery skew): an infrastructure fault, not a
        # caller authz verdict — 503, mirroring catalog/lineage security ("enabled but unavailable"),
        # so a valid admin bearer is not misreported as denied and 503-keyed monitoring sees the outage.
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None and authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise UnauthenticatedError("malformed bearer")
        try:
            # Off the loop: verify() does synchronous OIDC discovery + JWKS fetches (up to 15s) on a
            # cold cache or key rotation. Inline it stalled every in-flight request in this pod —
            # `service_kit.probes` is mounted on the same app, so the liveness probe queued behind a
            # bearer check and the kubelet could restart the pod mid-request. The sibling ingest door
            # fixed this as ING-02 and the fix did not travel here, which is why the hop now lives in
            # `service_kit.governed.oidc.verify_off_loop` instead of at the call site.
            token = await verify_off_loop(verifier, raw)
        except UnauthenticatedError:
            raise UnauthenticatedError("invalid token") from None
        except ProviderUnavailableError as exc:
            # The unwired-verifier branch's fact arriving later, so its body: the spec's `code` 17, not the fleet's.
            raise ServiceUnavailableError("authentication is enabled but unavailable") from exc
        return ProducerCaller(subject=token.sub)
    raise PermissionDeniedError("invalid or missing produce credential")


async def require_project_admin(fga_client: OpenFgaClient | None, caller: ProducerCaller, *, project: str | None, resource: str) -> None:
    """Authorize an admitted caller on ``project``: ``can_administer``, audited, fail closed.

    ``resource`` is what the call acts on, named in the record when ``project`` cannot be read. The
    service token passes, audited on that project. A person needs the relation on the project.
    ``project=None`` is a resource whose tenant cannot be read, and a person is refused it (503) rather
    than checked against the configured project, which would hand another tenant's run to that
    project's admins.
    """
    if caller.service is not None:
        audit("produce_service_token", ALLOW, subject=caller.service, resource=f"project:{project}" if project else resource)
        return
    if caller.subject is None:
        return
    if project is None:
        audit("can_administer", FAILURE, subject=caller.subject, resource=resource, reason="resource_project_unreadable")
        raise ServiceUnavailableError("the resource does not name its project, so it cannot be authorized")
    if fga_client is None:  # OIDC on but FGA unwired → fail closed, never an unauthorized act
        raise ServiceUnavailableError("authorization service is not available")
    await _require_admin(fga_client, user=caller.subject, obj=f"project:{project}")


async def administered_projects(fga_client: OpenFgaClient | None, caller: ProducerCaller, projects: Iterable[str]) -> frozenset[str]:
    """WHICH of ``projects`` the caller may see: one ``batch_check``, one audit line per project.

    The filtering twin of `require_project_admin`, shaped like ingest's `authorize_ingest_projects`: a
    per-project verdict filters rather than refuses, so a caller who administers none gets nothing back
    — a 403 for the whole call would say that some tenant has something to show. An authz outage is
    true of the whole call and raises 503, so it never reads as "you administer nothing".
    """
    unique = sorted(set(projects))
    if caller.service is not None:
        for project in unique:
            audit("produce_service_token", ALLOW, subject=caller.service, resource=f"project:{project}")
        return frozenset(unique)
    if caller.subject is None:
        return frozenset(unique)
    if not unique:
        return frozenset()
    if fga_client is None:
        raise ServiceUnavailableError("authorization service is not available")
    objects = [f"project:{project}" for project in unique]
    try:
        verdicts = await fga.batch_check(fga_client, user=caller.subject, relation="can_administer", objects=objects)
    except ServiceUnavailableError:
        audit("can_administer", FAILURE, subject=caller.subject, resource=",".join(objects), reason="authz_unavailable")
        raise ServiceUnavailableError("authorization service is not available") from None
    for obj in objects:
        audit("can_administer", ALLOW if verdicts.get(obj) else DENY, subject=caller.subject, resource=obj)
    return frozenset(project for project, obj in zip(unique, objects, strict=True) if verdicts.get(obj))


async def admit_caller(
    request: Request,
    settings: SettingsDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> ProducerCaller:
    """The door of a route acting on an EXISTING resource: authentication, and no ``?project=`` at all.

    Owner ruling 2026-09-25, "Authorize on the resource". The tenant of a stage run, a training watch or
    a stalled cell is recorded on it, so the route reads the resource and authorizes on the project it
    names (`require_project_admin` / `administered_projects`). A ``?project=`` here would hand that
    choice to the caller — an admin of one project naming their own to read or stop another's, measured
    2026-09-25 on `GET /cascade/stalled`.

    A refusal here comes before the resource is read, so it is recorded against the request's path.
    """
    return await _admit(
        request, settings, dapr_api_token=dapr_api_token, authorization=authorization, dapr_caller_app_id=dapr_caller_app_id, resource=request.url.path
    )


AdmittedCaller = Annotated[ProducerCaller, Depends(admit_caller)]


async def admit_config_read(request: Request, caller: AdmittedCaller) -> ProducerCaller:
    """The door of a DEPLOYMENT-CONFIG read: any caller `admit_caller` admits, and no project.

    Owner default 2026-09-26: the answer names no tenant, so a project check authorizes nothing, and a
    list gated tighter than the stage doors makes them its oracle. Lakekeeper gates
    `GET /management/v1/info` the same way. The admission is the decision, so it is what is recorded,
    against the path the door's own refusal names.
    """
    if caller.service is not None:
        audit("produce_service_token", ALLOW, subject=caller.service, resource=request.url.path)
    elif caller.subject is not None:
        audit("authn", SUCCESS, subject=caller.subject, resource=request.url.path)
    return caller


ConfigReader = Annotated[ProducerCaller, Depends(admit_config_read)]


async def authorize_produce(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    project: ProjectParam = None,
    # The INVOKING Dapr app-id — what separates "a service called me" from "the public front door
    # called me for a stranger". See `service_kit.governed.dapr_auth.is_public_caller`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> str | None:
    """Allow EITHER the Dapr app-api-token (service) OR a signed-in project admin (OIDC + can_administer).

    For a door whose ``?project=`` names the WRITE TARGET. A door acting on an existing resource takes
    `admit_caller` and authorizes on the project that resource records.

    RETURNS the verified subject on the human path, or ``None`` when the caller is a service (or the
    unconfigured-door hatch). This door is the LAST place a cascade's requester exists — by the time a
    silver or gold stage fails, the request is gone and the stage runner authors as a role. The value
    is only ever a TARGETING hint (it rides ``lance.originator`` into the notifications plane, which
    re-derives visibility per recipient at delivery); it authorizes nothing.

    ``project`` (#84) puts the admin gate on the REQUESTED project — the caller must administer the
    project it produces into; absent → ``produce_admin_project``. The service-token path stays
    project-BLIND: the shared token authenticates the service, not a tenant, so it may only produce into
    the configured project — a different requested project is refused (403); crossing tenants takes a
    user bearer, which gets the per-project FGA check."""
    target = project or settings.produce_admin_project
    obj = f"project:{target}"
    caller = await _admit(request, settings, dapr_api_token=dapr_api_token, authorization=authorization, dapr_caller_app_id=dapr_caller_app_id, resource=obj)
    # The shared token carries NO tenant identity, so it must never be trusted for an arbitrary requested
    # project: that would let any token holder produce into every tenant.
    if caller.service is not None and target != settings.produce_admin_project:
        audit("produce_service_token", DENY, subject=caller.service, resource=obj, reason="cross_project")
        raise PermissionDeniedError("the service token cannot produce into another project; use a project-admin bearer")
    await require_project_admin(fga_client, caller, project=target, resource=obj)
    return caller.subject


async def authenticate_subject(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """Verify WHO is calling and return their sub. Authorization is somebody else's job.

    `authorize_produce` fuses the two — it authenticates AND checks `can_administer` on a
    chart-configured project. That is right for the cascade head, whose whole permission question is
    "may you trigger this tenant's pipeline", and wrong for any door with a FINER rung. The medallion's
    promotion review is the case: `can_promote: validator` exists in the model precisely so that a
    validator who is NOT a project admin can approve a promotion, and reusing the produce gate makes
    the effective check admin AND validator — locking out the one person the rung was invented for
    (`docs/architecture/ingest-and-tier-movement.md` §4 rejects a door gated this way, in those words).

    Declares NO FGA client, deliberately: a dependency that carries one invites the same fusion back.

    There is no dev-open path. `authorize_produce` has one because a produce trigger needs no
    principal — the sub it returns is a targeting hint. A DECISION is the opposite: the subject is the
    record of who made it, and an anonymous approval is not an approval. A caller with no verified
    identity gets `None` and the door refuses.
    """
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if not authorization:
        return None
    if settings.oidc_enabled and verifier is None:
        # Enabled but unwired (startup/discovery skew) is an auth-layer OUTAGE, not a caller verdict —
        # the catalog/lineage security invariant, so a valid bearer is not misreported as denied.
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if verifier is None:
        return None
    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise UnauthenticatedError("malformed bearer")
    try:
        # Off the loop, for the same reason as `authorize_produce` above: this is the promotion
        # review's door, and it is a SECOND copy of the verify call — fixing only the produce door
        # would leave approve/reject stalling the worker.
        return (await verify_off_loop(verifier, raw)).sub
    except UnauthenticatedError:
        raise UnauthenticatedError("invalid token") from None
    except ProviderUnavailableError as exc:
        raise ServiceUnavailableError("authentication is enabled but unavailable") from exc


async def authorize_train(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    # Forwarded, not re-derived: `/train` delegates its whole decision to `authorize_produce`, so an
    # unforwarded caller id would silently restore the bypass on exactly this route while `/produce`
    # looked fixed — the delegation is what makes the two doors one door.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> str | None:
    """The ``/train`` door: the same dual-auth as produce, PINNED to the configured project.

    Training writes SINGLE-TENANT state (the model registry under the configured
    ``produce_admin_project``), so unlike ``/produce`` there is no per-tenant routing for a requested
    project to select — honoring a caller-supplied ``?project=`` here would let an admin of any OTHER
    project pass the gate while the run still lands in the configured tenant's registry (authorization
    scope must equal write scope). This dependency declares NO ``project`` query param, so a stray
    ``?project=`` is ignored and the admin check always targets ``produce_admin_project``.

    RETURNS the verified subject, exactly as ``authorize_produce`` does — the delegation covers the
    RESULT and not merely the checks. It declared ``None`` and discarded the sub the call below had
    already resolved, which is why the estate's most expensive door was also its most anonymous:
    training is submit-and-ack, so by the time the job fails there is no request left to ask who
    wanted it. The value targets and never authorizes; every decision above is unchanged."""
    return await authorize_produce(
        request,
        settings,
        fga_client,
        dapr_api_token=dapr_api_token,
        authorization=authorization,
        project=None,  # the explicit pin: always the configured produce_admin_project
        dapr_caller_app_id=dapr_caller_app_id,
    )


async def authorize_ingest_media(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> str | None:
    """The ``/ingest-media`` door: the same dual-auth as produce, PINNED like ``/train``.

    The media head's target is CONFIGURED (``media_head_enabled`` reads the source prefix and bronze
    table off settings), so there is no per-tenant routing for a caller-supplied project to select —
    declaring one would let an admin of any other project pass the gate while the bytes still land in
    the configured tenant's bronze. Authorization scope must equal write scope.

    RETURNS the verified subject. That is the whole point of replacing the token-only guard: media
    ingest is a fire-and-ack that hands the run to the cascade, so the door is the LAST place the
    requester's identity exists. Without it the media chain's events name a chart role literal, which
    addresses an inbox actor named after the role and reaches nobody — and ``notifiable()`` acks an
    untargetable event with a SUCCESS, so nothing reports the silence.
    """
    return await authorize_produce(
        request,
        settings,
        fga_client,
        dapr_api_token=dapr_api_token,
        authorization=authorization,
        project=None,  # the explicit pin: the media head's target is configured, not requested
        dapr_caller_app_id=dapr_caller_app_id,
    )
