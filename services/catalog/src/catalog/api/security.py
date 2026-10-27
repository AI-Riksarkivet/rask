"""Authentication at the catalog door: an OIDC bearer for a person or a Dex client, a projected service-account token for a service.

When OIDC is disabled (the default) this is a no-op and all routes stay open.
When enabled, it requires a valid bearer token on every route it guards and maps
auth failures to ``UnauthenticatedError`` (rendered as RFC 9457 problem+json, 401).

Fail-closed invariant: if OIDC is enabled in settings but the verifier a bearer needs was never
wired onto ``app.state`` (e.g. discovery failed at startup, or a deployment skew), we raise
``ServiceUnavailableError`` (503) rather than silently letting requests through. A
configured-but-broken auth layer must never degrade to open access.

A service is the Kubernetes service account its projected token names ([[LH-220]], ADR 0156 C5). The bearer's
unverified ``iss`` routes it: the cluster's service-account issuer goes to ``app.state.sa_oidc``, which
verifies it offline and answers the principal ``kubernetes~system:serviceaccount:<ns>:<sa>``, from any namespace;
every other issuer goes to the IdP, which refuses a token it did not sign (401), and answers a person as the
principal their Dex connector maps to, ``<idp-id>~<user id>`` ([[LH-063]]), and a Dex client signed in for itself as
the machine ``dex-client~<client id>`` (ADR 0181). A service-account token never falls
through to the IdP, no header names the caller, and OpenFGA alone decides what the principal may do.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from catalog.api.dependencies import SettingsDep
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.governed.deps import ANONYMOUS_SUBJECT
from service_kit.governed.idp_identity import IdpVerifier, PersonPrincipal
from service_kit.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from service_kit.governed.oidc import ProviderUnavailableError


# auto_error=False: we raise UnauthenticatedError ourselves so 401s are problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]

#: A verified caller: a person the IdP vouched for, or a machine, a service the cluster vouched for or a Dex client the
#: IdP vouched for. Authorization reads the principal key off either as `.subject`; `isinstance(token, ServicePrincipal)`
#: is the estate's one machine test.
type Caller = PersonPrincipal | ServicePrincipal


def _claims_issuer(bearer: str, issuer: str | None) -> bool:
    """Whether the bearer's unverified ``iss`` is ``issuer``: the routing question, for when no verifier was built."""
    if not issuer:
        return False
    try:
        claimed = jwt.decode(bearer, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == issuer.rstrip("/")


def _service_principal(verifier: ServiceAccountVerifier | None, bearer: str) -> ServicePrincipal:
    """The service a cluster-issued bearer proves, or the refusal that names why it proves none."""
    if verifier is None:
        # The settings name a service-account issuer and this bearer claims it, but no verifier was
        # built: a 503 keeps a broken boot visible instead of handing the token to the IdP.
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    try:
        principal = verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except UnauthenticatedError:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="invalid_token")
        raise
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject=principal.subject)
    return principal


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
) -> Caller | None:
    """Authenticate the request: a projected service-account token (a service) or an OIDC bearer (a person).

    The service-account verifier is chosen by the token's unverified ``iss`` and then checks the
    signature, audience and expiry; every account in the cluster whose token carries this door's audience
    authenticates, as its own subject, and is authorized by its tuples alone. ``dapr-api-token`` is not
    read here: it proves a sidecar delivered a request and names nobody.
    """
    if not settings.oidc_enabled:
        return None

    bearer = credentials.credentials if credentials is not None else None
    sa_verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if bearer and (sa_verifier.issued(bearer) if sa_verifier is not None else _claims_issuer(bearer, settings.sa_issuer)):
        return _service_principal(sa_verifier, bearer)

    verifier: IdpVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # OIDC is enabled but no verifier is available: fail closed, never open.
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if not bearer:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="missing_token")
        raise UnauthenticatedError("Missing bearer token")
    try:
        token = verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        # The IdP or this deployment's view of it failed, not the caller's bearer: the missing-verifier
        # branch's fact, so its reason and its body — the spec's `code` 17, the URL left to the log.
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except Exception:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="invalid_token")
        raise
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject=token.subject)  # #41 record the authenticated principal
    return token


#: The authenticated caller (``None`` when OIDC is disabled). Endpoints that need claims depend on
#: this and narrow with ``isinstance``; router-level use enforces authentication.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]


def current_subject(token: CurrentToken) -> str:
    """The verified caller as the estate's subject id: its principal key, or ``anon`` with OIDC off."""
    return token.subject if token is not None else ANONYMOUS_SUBJECT


CurrentSubject = Annotated[str, Depends(current_subject)]
