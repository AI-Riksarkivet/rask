"""Authentication dependency for the lineage read + ingest endpoints.

When OIDC is disabled (the default) this is a no-op and all routes stay open. When enabled, every caller presents a
verified bearer, and the bearer's unverified issuer decides which verifier answers it:

- the cluster's service-account issuer (`RASK_SA_ISSUER`): a pod's projected token, verified offline against the
  cluster's key set; whatever its namespace it is the subject `kubernetes~system:serviceaccount:<ns>:<sa>`
  (`lakehouse.governed.machine_identity`, [[LH-220]], ADR 0156 C5). It comes back a `ServicePrincipal` and never
  falls through to the IdP;
- anything else: the IdP's verifier (`IdpVerifier`, the same one the catalog uses), which answers a person as the
  principal the deployment's naming rule reads from the token, `<idp-id>~<subject>` ([[XC-214]], [[LH-063]]), and a
  client signed in for itself as the `ServicePrincipal` `<idp-id>-client~<client id>` (ADR 0181, ADR 0208).

Either way the caller is authorized by FGA as its principal key, `.subject`. Nothing a caller asserts in a header names it,
and a service-account token's audience is lineage's own (`rask-lineage`), so a token minted for another door is
refused here.

Fail-closed invariant: if OIDC is enabled but the verifier that would answer a bearer was never wired onto
``app.state`` (startup/discovery skew), the door raises ``ServiceUnavailableError`` (503) rather than letting the
request through or handing it to the other verifier.
"""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lakehouse.governed.admission import admitted_caller, caller_judge
from lakehouse.governed.door_caller import caller_of
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal
from lineage.api.dependencies import SettingsDep
from lineage.core.config import LineageSettings, get_settings


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


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
) -> Caller | None:
    """Authenticate the caller: a person's IdP bearer, or a service's projected service-account token.

    The caller `judge` admitted, when it ran before the body; otherwise judged here. ``None`` when OIDC is off, the open
    dev default.
    """
    judged, caller = admitted_caller(request)
    if judged:
        return cast("Caller | None", caller)
    return _authenticated(request, settings, credentials.credentials if credentials is not None and credentials.credentials else None)


def _authenticated(request: Request, settings: LineageSettings, bearer: str | None) -> Caller | None:
    """The caller ``bearer`` proves, or ``None`` with OIDC off: judged, and recorded, as at every door (`door_caller`)."""
    return caller_of(request.app.state, settings, bearer)


#: The authenticated caller — a ``PersonPrincipal``, a ``ServicePrincipal``, or ``None`` when OIDC is off.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]
#: Every door that depends on `authenticate` judges its caller from the headers before its body is read
#: (`lakehouse.governed.admission`): the intake's JSON body is never read, let alone parsed, for a caller with no token.
judge = caller_judge(gate=authenticate, settings=get_settings, authenticated=_authenticated)

__all__ = ["Caller", "CurrentToken", "PersonPrincipal", "ServicePrincipal", "authenticate"]
