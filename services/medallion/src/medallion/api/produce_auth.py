"""Dual-auth for the producer's doors: a service by its projected service-account token, or a signed-in person.

The cascade head must never be forgeable (a bronze-write event fabricates provenance). Two kinds of caller
pass this door, and both are AUTHORIZED the same way:
  * a SERVICE presents the token the kubelet projects for its own service account with audience
    `rask-medallion`; the door verifies it offline against the cluster issuer and maps the account to its
    subject (`service_door`, [[LH-220]], D1);
  * a PERSON presents an OIDC bearer, so the web BFF forwards the user's own token and the web pod holds no
    credential of its own for this door.
Either subject must then hold ``can_administer`` on the project the call acts on. A service is held to its
own tenant's runs by FGA exactly like a person; nothing here admits a caller tenant-blind.

WHICH PROJECT THE ADMIN CHECK NAMES depends on the door (owner ruling 2026-09-25, "Authorize on the
resource"). A door whose ``?project=`` is its WRITE TARGET checks that project (`authorize_produce`). A door
acting on an EXISTING resource authenticates only (`admit_caller`) and checks the project the resource
records (`require_project_admin`, `administered_projects`), so no caller-chosen value can move its gate.
A read of DEPLOYMENT config, the same for every tenant, admits any authenticated caller and checks no
project (`admit_config_read`).

Fail-closed at every step: a door with nothing configured to authenticate a caller is a refusal unless an
operator acknowledged it (`service_door.refuse_unauthenticatable_door`); a bearer is REQUIRED and must be
valid (else 401) AND resolve to a project admin (else 403), with an OpenFGA outage failing to 503 — never a
silent allow; a request carrying no bearer is 403. A verifier that is configured but not wired
(startup/discovery skew) is 503 for a bearer of its issuer — an auth-layer outage, not a caller verdict
(the catalog/lineage ``security.py`` invariant). The ``dapr-api-token`` daprd stamps on an invocation names
nobody, so this door does not read it. Every ``can_administer`` allow/deny/outage is audited on the
``lance.audit`` stream (#41), naming the project the call acts on, or the path a config read reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.api.service_door import bearer_token, refuse_unauthenticatable_door, service_issued, verify_service
from medallion.core.config import MedallionSettings
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, SUCCESS, audit
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

    ``subject`` is the verified subject, a person's or a service's, still to be authorized on a project.
    ``service_account`` is set when the cluster vouched for the caller: the subject is then a service's,
    authorized exactly like a person's, and it is never a person a notification could address. Neither set
    is the acknowledged-open door, which verified nobody.
    """

    model_config = ConfigDict(frozen=True)

    subject: str | None = None
    service_account: str | None = None


async def _admit(
    request: Request,
    settings: MedallionSettings,
    *,
    authorization: str | None,
    dapr_caller_app_id: str | None,
    resource: str,
) -> ProducerCaller:
    """AUTHENTICATE a producer caller: a service or a person, verified and not yet authorized.

    The caller of this function authorizes on the write target (`authorize_produce`) or on the resource
    (`require_project_admin` / `administered_projects`). ``resource`` names what this door's own refusal is
    recorded against.
    """
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    # A door that can verify nobody is refused unless an operator said it may run open, through the same
    # acknowledgement every service's boot asks for. Taken, the caller is admitted with no subject, because
    # no verified subject exists on that path, never a guess.
    if not settings.oidc_enabled and not settings.sa_issuer and getattr(request.app.state, "sa_oidc", None) is None:
        refuse_unauthenticatable_door(settings)
        return ProducerCaller()
    if not authorization:
        raise PermissionDeniedError("invalid or missing produce credential")
    raw = bearer_token(authorization)
    if service_issued(request, settings, raw):
        principal = await verify_service(request, raw, caller_app_id=dapr_caller_app_id, resource=resource)
        return ProducerCaller(subject=principal.sub, service_account=principal.service_account)
    # Human path: a signed-in person. Only when OIDC is configured + a verifier is wired.
    if settings.oidc_enabled and verifier is None:
        # OIDC enabled but no verifier wired (startup/discovery skew): an infrastructure fault, not a
        # caller authz verdict — 503, mirroring catalog/lineage security ("enabled but unavailable"),
        # so a valid admin bearer is not misreported as denied and 503-keyed monitoring sees the outage.
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None:
        try:
            # Off the loop: verify() does synchronous OIDC discovery + JWKS fetches (up to 15s) on a
            # cold cache or key rotation. Inline it stalled every in-flight request in this pod —
            # `service_kit.probes` is mounted on the same app, so the liveness probe queued behind a
            # bearer check and the kubelet could restart the pod mid-request.
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

    A service and a person are checked alike, on the subject `_admit` verified. ``resource`` is what the
    call acts on, named in the record when ``project`` cannot be read. ``project=None`` is a resource whose
    tenant cannot be read, and the caller is refused it (503) rather than checked against the configured
    project, which would hand another tenant's run to that project's admins.
    """
    if caller.subject is None:
        return
    if project is None:
        audit("can_administer", FAILURE, subject=caller.subject, resource=resource, reason="resource_project_unreadable")
        raise ServiceUnavailableError("the resource does not name its project, so it cannot be authorized")
    if fga_client is None:  # a verified caller but FGA unwired → fail closed, never an unauthorized act
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
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> ProducerCaller:
    """The door of a route acting on an EXISTING resource: authentication, and no ``?project=`` at all.

    Owner ruling 2026-09-25, "Authorize on the resource". The tenant of a stage run, a training run or
    a stalled cell is recorded on it, so the route reads the resource and authorizes on the project it
    names (`require_project_admin` / `administered_projects`). A ``?project=`` here would hand that
    choice to the caller — an admin of one project naming their own to read or stop another's, measured
    2026-09-25 on `GET /cascade/stalled`.

    A refusal here comes before the resource is read, so it is recorded against the request's path.
    """
    return await _admit(request, settings, authorization=authorization, dapr_caller_app_id=dapr_caller_app_id, resource=request.url.path)


AdmittedCaller = Annotated[ProducerCaller, Depends(admit_caller)]


async def admit_config_read(request: Request, caller: AdmittedCaller) -> ProducerCaller:
    """The door of a DEPLOYMENT-CONFIG read: any caller `admit_caller` admits, and no project.

    Owner default 2026-09-26: the answer names no tenant, so a project check authorizes nothing, and a
    list gated tighter than the stage doors makes them its oracle. Lakekeeper gates
    `GET /management/v1/info` the same way. The admission is the decision, so it is what is recorded,
    against the path the door's own refusal names.
    """
    if caller.subject is not None:
        audit("authn", SUCCESS, subject=caller.subject, resource=request.url.path)
    return caller


ConfigReader = Annotated[ProducerCaller, Depends(admit_config_read)]


async def authorize_produce(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    authorization: Annotated[str | None, Header()] = None,
    project: ProjectParam = None,
    # The INVOKING Dapr app-id — what separates "a service called me" from "the public front door
    # called me for a stranger". See `service_kit.governed.dapr_auth.is_public_caller`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> str | None:
    """Allow a service or a signed-in person who holds ``can_administer`` on the project produced into.

    For a door whose ``?project=`` names the WRITE TARGET. A door acting on an existing resource takes
    `admit_caller` and authorizes on the project that resource records.

    ``project`` (#84) puts the admin gate on the REQUESTED project — the caller must administer the
    project it produces into; absent → ``produce_admin_project``. A service is checked on it exactly like
    a person, so it produces only into a tenant its own subject administers.

    RETURNS the verified person's subject, or ``None`` when the caller is a service (or the
    acknowledged-open door). This door is the LAST place a cascade's requester exists — by the time a
    silver or gold stage fails, the request is gone and the stage runner authors as a role. The value
    is only ever a TARGETING hint (it rides ``lance.originator`` into the notifications plane, which
    re-derives visibility per recipient at delivery); it authorizes nothing, and a service's subject
    addresses no inbox, so it is never one."""
    target = project or settings.produce_admin_project
    obj = f"project:{target}"
    caller = await _admit(request, settings, authorization=authorization, dapr_caller_app_id=dapr_caller_app_id, resource=obj)
    await require_project_admin(fga_client, caller, project=target, resource=obj)
    return None if caller.service_account else caller.subject


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
        authorization=authorization,
        project=None,  # the explicit pin: always the configured produce_admin_project
        dapr_caller_app_id=dapr_caller_app_id,
    )


async def authorize_ingest_media(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
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
        authorization=authorization,
        project=None,  # the explicit pin: the media head's target is configured, not requested
        dapr_caller_app_id=dapr_caller_app_id,
    )
