"""OIDC authentication dependency.

When OIDC is disabled (the default) this is a no-op and all routes stay open.
When enabled, it requires a valid bearer token on every route it guards and maps
auth failures to ``UnauthenticatedError`` (rendered as RFC 9457 problem+json, 401).

Fail-closed invariant: if OIDC is enabled in settings but the verifier was never
wired onto ``app.state`` (e.g. discovery failed at startup, or a deployment skew),
we raise ``ServiceUnavailableError`` (503) rather than silently letting requests
through. A configured-but-broken auth layer must never degrade to open access.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from catalog.api.dependencies import SettingsDep
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.oidc import IDToken, OIDCVerifier


# auto_error=False: we raise UnauthenticatedError ourselves so 401s are problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def authenticate(request: Request, settings: SettingsDep, credentials: _CredentialsDep) -> IDToken | None:
    """Authenticate the request, returning the parsed token (or ``None`` when OIDC is off)."""
    if not settings.oidc_enabled:
        return None
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # OIDC is enabled but no verifier is available: fail closed, never open.
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if credentials is None or not credentials.credentials:
        audit("authn", FAILURE, reason="missing_token")
        raise UnauthenticatedError("Missing bearer token")
    try:
        token = verifier.verify(credentials.credentials)
    except Exception:
        audit("authn", FAILURE, reason="invalid_token")
        raise
    audit("authn", SUCCESS, subject=token.sub)  # #41 record the authenticated principal
    return token


#: Token of the authenticated caller (``None`` when OIDC is disabled). Endpoints that
#: need claims can depend on this; router-level use enforces authentication.
CurrentToken = Annotated[IDToken | None, Depends(authenticate)]


def raw_bearer(credentials: _CredentialsDep) -> str | None:
    """The raw bearer JWT string (scheme-stripped), or ``None`` when no bearer is present.

    For routes that must FORWARD the caller's token rather than only verify it — e.g. credential vending's
    web_identity flow re-presents it to the object store (AssumeRoleWithWebIdentity). Reuses the single
    ``HTTPBearer`` seam, so parsing matches :func:`authenticate` (case-insensitive scheme — ``BEARER …`` too).
    """
    return credentials.credentials if credentials is not None else None


#: The caller's raw bearer JWT (``None`` when absent) — for forwarding, not verification.
RawBearerToken = Annotated[str | None, Depends(raw_bearer)]
