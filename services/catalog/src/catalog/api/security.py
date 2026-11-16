"""Authentication at the catalog door: an OIDC bearer for a person or a client, a projected service-account token for a service.

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
principal the deployment's naming rule reads from it, ``<idp-id>~<subject>`` ([[XC-214]], [[LH-063]]), and a client
signed in for itself as the machine ``<idp-id>-client~<client id>`` (ADR 0181, ADR 0208), each under its own issuer's
idp id. A service-account token never falls
through to the IdP, no header names the caller, and OpenFGA alone decides what the principal may do.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from catalog.api.dependencies import SettingsDep
from catalog.core.config import Settings, get_settings
from lakehouse.admission import admitted_caller, caller_judge
from lakehouse.governed.deps import ANONYMOUS_SUBJECT
from lakehouse.governed.door_caller import caller_of
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal


# auto_error=False: we raise UnauthenticatedError ourselves so 401s are problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]

#: A verified caller: a person the IdP vouched for, or a machine, a service the cluster vouched for or a Dex client the
#: IdP vouched for. Authorization reads the principal key off either as `.subject`; `isinstance(token, ServicePrincipal)`
#: is the estate's one machine test.
type Caller = PersonPrincipal | ServicePrincipal


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
) -> Caller | None:
    """Authenticate the request: a projected service-account token (a service) or an OIDC bearer (a person).

    The caller `judge` admitted, when it ran before the body; otherwise judged here.
    """
    judged, caller = admitted_caller(request)
    if judged:
        return cast("Caller | None", caller)
    return _authenticated(request, settings, credentials.credentials if credentials is not None else None)


def _authenticated(request: Request, settings: Settings, bearer: str | None) -> Caller | None:
    """The caller ``bearer`` proves, or ``None`` with OIDC off: judged, and recorded, as at every door (`door_caller`).

    Every account in the cluster whose token carries this door's audience authenticates, as its own subject, and is
    authorized by its tuples alone.
    """
    return caller_of(request.app.state, settings, bearer)


#: The authenticated caller (``None`` when OIDC is disabled). Endpoints that need claims depend on
#: this and narrow with ``isinstance``; router-level use enforces authentication.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]
#: Every door that depends on `authenticate` judges its caller from the headers before its body is read
#: (`lakehouse.admission`), so no caller without a token makes this service buffer one.
judge = caller_judge(gate=authenticate, settings=get_settings, authenticated=_authenticated)


def current_subject(token: CurrentToken) -> str:
    """The verified caller as the estate's subject id: its principal key, or ``anon`` with OIDC off."""
    return token.subject if token is not None else ANONYMOUS_SUBJECT


CurrentSubject = Annotated[str, Depends(current_subject)]
