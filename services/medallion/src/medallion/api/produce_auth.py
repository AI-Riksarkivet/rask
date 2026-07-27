"""Dual-auth for ``POST /produce`` (#64): the DAPR app-api-token OR a signed-in project admin.

The cascade head must never be forgeable (a raw-write event fabricates provenance), so the existing
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

import os
import secrets
from typing import Annotated

from common import fga
from common.audit import ALLOW, DENY, FAILURE, audit
from common.oidc import OIDCVerifier
from common.warehouse_registry import PROJECT_PATTERN
from fastapi import Header, HTTPException, Query, Request
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from medallion.api.dependencies import FgaClientDep, SettingsDep


#: The optional per-tenant project (#84) — shared by the auth gate and the /produce route (FastAPI
#: deduplicates the identically-declared query param). Pattern-bound so an unsafe id 422s at the edge.
ProjectParam = Annotated[str | None, Query(min_length=1, max_length=64, pattern=PROJECT_PATTERN)]


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
        raise HTTPException(status_code=503, detail="authorization service is not available") from None
    audit("can_administer", ALLOW if allowed else DENY, subject=user, resource=obj)
    if not allowed:
        raise HTTPException(status_code=403, detail="produce needs project admin (can_administer) or the service token")


async def authorize_produce(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    project: ProjectParam = None,
) -> None:
    """Allow EITHER the Dapr app-api-token (service) OR a signed-in project admin (OIDC + can_administer).

    ``project`` (#84) moves the admin gate onto the REQUESTED project — the caller must administer the
    project it produces into, not the fixed configured one; absent → ``produce_admin_project`` exactly as
    before. The service-token path stays project-BLIND: the shared token authenticates the service, not
    a tenant, so it may only produce into the configured project — a different requested project is
    refused (403); crossing tenants takes a user bearer, which gets the per-project FGA check."""
    expected = os.environ.get("APP_API_TOKEN")
    # Dev: no service token configured → open, exactly as require_dapr_token was a no-op.
    if not expected:
        return
    obj = f"project:{project or settings.produce_admin_project}"
    # Service-to-service path: a matching Dapr app-api-token. The shared token carries NO tenant
    # identity, so it must never be trusted for an arbitrary requested project — that would let any
    # token holder produce into every tenant. Configured project (or none) only; else 403. The door
    # decision is audited like the human one (#41); the shared token names no principal, so the
    # subject is the fixed "service" marker.
    if dapr_api_token and secrets.compare_digest(dapr_api_token.encode(), expected.encode()):
        if project and project != settings.produce_admin_project:
            audit("produce_service_token", DENY, subject="service", resource=obj, reason="cross_project")
            raise HTTPException(
                status_code=403,
                detail="the service token cannot produce into another project; use a project-admin bearer",
            )
        audit("produce_service_token", ALLOW, subject="service", resource=obj)
        return
    # Human path: a signed-in project admin. Only when OIDC is configured + a verifier is wired.
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if settings.oidc_enabled and verifier is None and authorization:
        # OIDC enabled but no verifier wired (startup/discovery skew): an infrastructure fault, not a
        # caller authz verdict — 503, mirroring catalog/lineage security ("enabled but unavailable"),
        # so a valid admin bearer is not misreported as denied and 503-keyed monitoring sees the outage.
        raise HTTPException(status_code=503, detail="authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None and authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise HTTPException(status_code=401, detail="malformed bearer")
        try:
            token = verifier.verify(raw)
        except UnauthenticatedError:
            raise HTTPException(status_code=401, detail="invalid token") from None
        if fga_client is None:  # OIDC on but FGA unwired → fail closed, never an unauthorized trigger
            raise HTTPException(status_code=503, detail="authorization service is not available")
        await _require_admin(fga_client, user=token.sub, obj=obj)
        return
    raise HTTPException(status_code=403, detail="invalid or missing produce credential")


async def authorize_train(
    request: Request,
    settings: SettingsDep,
    fga_client: FgaClientDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """The ``/train`` door: the same dual-auth as produce, PINNED to the configured project.

    Training writes SINGLE-TENANT state (the model registry under the configured
    ``produce_admin_project``), so unlike ``/produce`` there is no per-tenant routing for a requested
    project to select — honoring a caller-supplied ``?project=`` here would let an admin of any OTHER
    project pass the gate while the run still lands in the configured tenant's registry (authorization
    scope must equal write scope). This dependency declares NO ``project`` query param, so a stray
    ``?project=`` is ignored and the admin check always targets ``produce_admin_project``."""
    await authorize_produce(
        request,
        settings,
        fga_client,
        dapr_api_token=dapr_api_token,
        authorization=authorization,
        project=None,  # the explicit pin: always the configured produce_admin_project
    )
