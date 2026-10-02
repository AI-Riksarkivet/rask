"""Authentication at the catalog door: an OIDC bearer for a person, a projected service-account token for a service.

When OIDC is disabled (the default) this is a no-op and all routes stay open.
When enabled, it requires a valid bearer token on every route it guards and maps
auth failures to ``UnauthenticatedError`` (rendered as RFC 9457 problem+json, 401).

Fail-closed invariant: if OIDC is enabled in settings but the verifier a bearer needs was never
wired onto ``app.state`` (e.g. discovery failed at startup, or a deployment skew), we raise
``ServiceUnavailableError`` (503) rather than silently letting requests through. A
configured-but-broken auth layer must never degrade to open access.

A service is the Kubernetes service account its projected token names ([[LH-220]], D1). The bearer's
unverified ``iss`` routes it: the cluster's service-account issuer goes to ``app.state.sa_oidc``, which
verifies it offline and maps its full username to a subject; every other issuer goes to the IdP. A
service-account token never falls through to the IdP, and no header names the caller.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from catalog.api.dependencies import SettingsDep
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.deps import ANONYMOUS_SUBJECT
from service_kit.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from service_kit.governed.oidc import IDToken, OIDCVerifier, ProviderUnavailableError


# auto_error=False: we raise UnauthenticatedError ourselves so 401s are problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]

#: A verified caller: a person's IdP token, or a service the cluster vouched for. Authorization reads
#: `.sub` off either; `isinstance(token, ServicePrincipal)` is the estate's one machine test.
type Principal = IDToken | ServicePrincipal


def _claims_issuer(bearer: str, issuer: str | None) -> bool:
    """Whether the bearer's unverified ``iss`` is ``issuer``: the routing question, for when no verifier was built."""
    if not issuer:
        return False
    try:
        claimed = jwt.decode(bearer, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == issuer.rstrip("/")


def _service_principal(verifier: ServiceAccountVerifier | None, bearer: str, caller_app_id: str | None) -> ServicePrincipal:
    """The service a cluster-issued bearer proves, or the refusal that names why it proves none."""
    if verifier is None:
        # The settings name a service-account issuer and this bearer claims it, but no verifier was
        # built: a 503 keeps a broken boot visible instead of handing the token to the IdP.
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if is_public_caller(caller_app_id):
        # The public front door forwards a stranger's request through Dapr; a service principal is
        # never something it may present on their behalf. The gateway strips the header a client
        # could forge, which is what makes this check mean anything.
        audit("authn", FAILURE, reason="public_caller")
        raise PermissionDeniedError(f"{caller_app_id!r} is a public front door: a service token is not accepted through it")
    try:
        principal = verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except UnauthenticatedError:
        audit("authn", FAILURE, reason="invalid_token")
        raise
    audit("authn", SUCCESS, subject=principal.sub)
    return principal


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
    # The INVOKING Dapr app-id — what separates a service from the PUBLIC front door invoking on a
    # stranger's behalf. See `service_kit.governed.dapr_auth.is_public_caller`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Authenticate the request: a projected service-account token (a service) or an OIDC bearer (a person).

    The service-account verifier is chosen by the token's unverified ``iss`` and then checks the
    signature, audience, expiry and the exact full-username map; signature and audience alone accept
    every account in the cluster that carries this door's audience (measured 2026-10-02, the P5.3 c0
    probe), so the map is what binds a token to one subject. ``dapr-api-token`` is not read here: it
    proves a sidecar delivered a request and names nobody.
    """
    if not settings.oidc_enabled:
        return None

    bearer = credentials.credentials if credentials is not None else None
    sa_verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if bearer and (sa_verifier.issued(bearer) if sa_verifier is not None else _claims_issuer(bearer, settings.sa_issuer)):
        return _service_principal(sa_verifier, bearer, dapr_caller_app_id)

    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # OIDC is enabled but no verifier is available: fail closed, never open.
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if not bearer:
        audit("authn", FAILURE, reason="missing_token")
        raise UnauthenticatedError("Missing bearer token")
    try:
        token = verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        # The IdP or this deployment's view of it failed, not the caller's bearer: the missing-verifier
        # branch's fact, so its reason and its body — the spec's `code` 17, the URL left to the log.
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except Exception:
        audit("authn", FAILURE, reason="invalid_token")
        raise
    audit("authn", SUCCESS, subject=token.sub)  # #41 record the authenticated principal
    return token


#: The authenticated caller (``None`` when OIDC is disabled). Endpoints that need claims depend on
#: this and narrow with ``isinstance``; router-level use enforces authentication.
CurrentToken = Annotated[Principal | None, Depends(authenticate)]


def current_subject(token: CurrentToken) -> str:
    """The verified caller as the estate's subject id: the principal's ``sub``, or ``anon`` with OIDC off."""
    return token.sub if token is not None else ANONYMOUS_SUBJECT


CurrentSubject = Annotated[str, Depends(current_subject)]


def raw_bearer(credentials: _CredentialsDep, token: CurrentToken) -> str | None:
    """A PERSON's raw bearer JWT string (scheme-stripped), or ``None`` when there is none to forward.

    For routes that must FORWARD the caller's token rather than only verify it — e.g. credential vending's
    web_identity flow re-presents it to the object store (AssumeRoleWithWebIdentity). Reuses the single
    ``HTTPBearer`` seam, so parsing matches :func:`authenticate` (case-insensitive scheme — ``BEARER …`` too).
    A service's projected token answers ``None``: it was minted for this door's audience alone, and
    re-presenting it to the object store would hand a credential to a party it was never issued for.
    """
    if credentials is None or isinstance(token, ServicePrincipal):
        return None
    return credentials.credentials


#: The caller's raw bearer JWT (``None`` when absent or a service's) — for forwarding, not verification.
RawBearerToken = Annotated[str | None, Depends(raw_bearer)]
