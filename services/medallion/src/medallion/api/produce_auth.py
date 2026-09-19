"""Dual-auth for ``POST /produce`` (#64): the DAPR app-api-token OR a signed-in project admin.

The cascade head must never be forgeable (a bronze-write event fabricates provenance), so the existing
service-to-service guard — the shared app-api-token — is kept UNCHANGED. This adds a SECOND, human door:
a signed-in OIDC user who holds ``can_administer`` on the project may trigger produce, so the web BFF can
forward the *user's* bearer and the web pod never holds the service token (no secrets-posture change).

Fail-closed at every step: no service token configured is dev-open (matching the old ``require_dapr_token``);
a matching Dapr token passes (service path) — but only for the CONFIGURED project, since the shared token
carries no tenant identity (crossing tenants takes a user bearer); otherwise an OIDC bearer is REQUIRED
and must be valid (else 401) AND resolve to a project admin (else 403), with an OpenFGA outage failing to
503 — never a silent allow; a request carrying neither credential is 403. OIDC enabled with NO verifier
wired (startup/discovery skew) is 503 for a bearer-presenting caller — an auth-layer outage, not a caller
verdict (the catalog/lineage ``security.py`` invariant). Every door decision — the ``can_administer``
allow/deny/outage AND the service-token acceptance — is audited on the ``lance.audit`` stream (#41).
"""

from __future__ import annotations

import secrets
from typing import Annotated, Protocol

from fastapi import Header, Query, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from medallion.api.dependencies import FgaClientDep, SettingsDep
from service_kit.governed import dapr_auth, fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.oidc import OIDCVerifier, verify_off_loop
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
        raise PermissionDeniedError("produce needs project admin (can_administer) or the service token")


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

    RETURNS the verified subject on the human path, or ``None`` when the caller is a service (or dev-open).
    It used to return nothing, which is why a cascade this person started could never name them: this door
    is the LAST place their identity exists — by the time a silver or gold stage fails, the request is
    gone and the stage runner authors as a role. The value is only ever a TARGETING hint (it rides
    ``lance.originator`` into the notifications plane, which re-derives visibility per recipient at
    delivery); it authorizes nothing, and every authorization decision above is unchanged.

    ``project`` (#84) moves the admin gate onto the REQUESTED project — the caller must administer the
    project it produces into, not the fixed configured one; absent → ``produce_admin_project`` exactly as
    before. The service-token path stays project-BLIND: the shared token authenticates the service, not
    a tenant, so it may only produce into the configured project — a different requested project is
    refused (403); crossing tenants takes a user bearer, which gets the per-project FGA check."""
    expected = _expected_app_token(settings)
    # Dev: no service token configured → open, exactly as require_dapr_token was a no-op. No verified
    # subject exists on this path, so there is no originator to carry — `None`, never a guess.
    if not expected:
        return None
    obj = f"project:{project or settings.produce_admin_project}"
    # Service-to-service path: a matching Dapr app-api-token. The shared token carries NO tenant
    # identity, so it must never be trusted for an arbitrary requested project — that would let any
    # token holder produce into every tenant. Configured project (or none) only; else 403. The door
    # decision is audited like the human one (#41); the shared token names no principal, so the
    # subject is the fixed "service" marker.
    # THE MEASURED BYPASS. The gateway forwards through Dapr service invocation and the callee's
    # daprd stamps a valid `dapr-api-token` on the way in, so an ANONYMOUS public request reaches this
    # line already holding the estate's service credential. Measured on the sibling ingest door: 403
    # straight to the pod, 202 through the gateway. `/produce` writes bronze$events, fabricates
    # OpenLineage provenance and fires the whole bronze->silver->gold cascade; `/train` spends GPU.
    # A public caller therefore gets NO service-token path — it falls through to the bearer below.
    from_public_door = is_public_caller(dapr_caller_app_id)
    if from_public_door and dapr_api_token and not authorization:
        audit("produce_service_token", DENY, subject=f"service:{dapr_caller_app_id}", resource=obj, reason="public_caller")
        raise PermissionDeniedError(
            f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller — sign in and retry"
        )
    if not from_public_door and dapr_api_token and secrets.compare_digest(dapr_api_token.encode(), expected.encode()):
        if project and project != settings.produce_admin_project:
            audit("produce_service_token", DENY, subject=f"service:{dapr_caller_app_id or 'direct'}", resource=obj, reason="cross_project")
            raise PermissionDeniedError("the service token cannot produce into another project; use a project-admin bearer")
        audit("produce_service_token", ALLOW, subject=f"service:{dapr_caller_app_id or 'direct'}", resource=obj)
        return None
    # Human path: a signed-in project admin. Only when OIDC is configured + a verifier is wired.
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
        if fga_client is None:  # OIDC on but FGA unwired → fail closed, never an unauthorized trigger
            raise ServiceUnavailableError("authorization service is not available")
        await _require_admin(fga_client, user=token.sub, obj=obj)
        return token.sub
    raise PermissionDeniedError("invalid or missing produce credential")


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
