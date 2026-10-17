"""Authentication dependency for the lineage read + ingest endpoints.

When OIDC is disabled (the default) this is a no-op and all routes stay open. When enabled, every caller presents a
verified bearer, and the bearer's unverified issuer decides which verifier answers it:

- the cluster's service-account issuer (`RASK_SA_ISSUER`): a pod's projected token, verified offline against the
  cluster's key set; whatever its namespace it is the subject `kubernetes~system:serviceaccount:<ns>:<sa>`
  (`service_kit.governed.machine_identity`, [[LH-220]], ADR 0156 C5). It comes back a `ServicePrincipal` and never
  falls through to the IdP;
- anything else: the IdP's verifier (`PersonVerifier`, the same one the catalog uses), which answers a person as the
  principal their Dex connector maps to, `<idp-id>~<user id>` ([[LH-063]]).

Either way the caller is authorized by FGA as its principal key, `.subject`. Nothing a caller asserts in a header names it:
`dapr-api-token` proves only that a sidecar delivered a request (`require_dapr_token`, on the subscription routes),
and a service-account token's audience is lineage's own (`rask-lineage`), so a token minted for another door is
refused here.

Fail-closed invariant: if OIDC is enabled but the verifier that would answer a bearer was never wired onto
``app.state`` (startup/discovery skew), the door raises ``ServiceUnavailableError`` (503) rather than letting the
request through or handing it to the other verifier.
"""

from __future__ import annotations

from typing import Annotated, Protocol

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from lineage.api.dependencies import SettingsDep
from lineage.core.config import LineageSettings
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from service_kit.governed.oidc import ProviderUnavailableError
from service_kit.governed.person_identity import PersonPrincipal, PersonVerifier


# auto_error=False: we raise UnauthenticatedError ourselves so 401s render as problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


class Caller(Protocol):
    """What the ingest authz layer actually needs of a caller: a principal key to attribute + authorize.

    Both :func:`~lineage.api.fga_deps.enforce_author` and
    :func:`~lineage.api.fga_deps.enforce_output_authz` read only ``.subject``, so a :class:`PersonPrincipal`
    and a :class:`ServicePrincipal` are interchangeable there.
    """

    @property
    def subject(self) -> str: ...


def _service_account_issued(token: str, settings: LineageSettings, verifier: ServiceAccountVerifier | None) -> bool:
    """Whether the bearer claims the service-account issuer: the routing question, asked before any signature check.

    With the verifier absent the configured issuer still answers it, so a service-account token reaching a door whose
    verifier failed to build is refused as an outage rather than handed to the IdP's verifier.
    """
    if verifier is not None:
        return verifier.issued(token)
    if not settings.sa_issuer:
        return False
    try:
        claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == settings.sa_issuer.rstrip("/")


def _service_account(token: str, verifier: ServiceAccountVerifier | None) -> ServicePrincipal:
    """The service a projected token proves, rendered in the door's problem vocabulary."""
    if verifier is None:
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    try:
        principal = verifier.verify(token)
    except ProviderUnavailableError as exc:
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except UnauthenticatedError:
        audit("authn", FAILURE, reason="invalid_token")
        raise
    audit("authn", SUCCESS, subject=principal.subject, service_account=principal.service_account)
    return principal


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
) -> Caller | None:
    """Authenticate the caller: a person's IdP bearer, or a service's projected service-account token.

    Returns ``None`` when OIDC is off — the open dev default.
    """
    if not settings.oidc_enabled:
        return None
    bearer = credentials.credentials if credentials is not None and credentials.credentials else None
    sa_verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if bearer is not None and _service_account_issued(bearer, settings, sa_verifier):
        return _service_account(bearer, sa_verifier)
    verifier: PersonVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # Enabled but no verifier wired (startup/discovery skew): fail closed, never open.
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if bearer is None:
        raise UnauthenticatedError("Missing bearer token")
    try:
        return verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        # The missing-verifier branch's fact arriving later, so its body: the spec's `code` 17, not the fleet's.
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc


#: The authenticated caller — a ``PersonPrincipal``, a ``ServicePrincipal``, or ``None`` when OIDC is off.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]

__all__ = ["Caller", "CurrentToken", "PersonPrincipal", "ServicePrincipal", "authenticate"]
