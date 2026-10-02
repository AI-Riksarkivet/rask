"""A service at a medallion door is the Kubernetes service account its projected token names ([[LH-220]], D1).

Two doors read it. The producer's (`produce_auth`) admits a service beside a signed-in person and then
authorizes both the same way, on FGA. A stage runner's operator routes (`stage_ops`) admit the producer and
nobody else: the producer is where a person's request is authorized, and the stage runner is reachable over
its ClusterIP, so admitting any other account would let a caller step round that check.

A bearer is routed by the issuer it claims: one claiming the service-account issuer goes to that verifier
and never to Dex, so a token for another door, an account in another namespace, or one the door's
`RASK_SA_SUBJECTS` does not name is refused rather than tried elsewhere. Nothing else names a service — the
`dapr-api-token` daprd stamps on an invocation proves only that a sidecar delivered it.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from starlette.concurrency import run_in_threadpool

from medallion.api.dependencies import SettingsDep
from medallion.core.config import MedallionSettings
from service_kit.governed.audit import DENY, FAILURE, SUCCESS, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from service_kit.governed.oidc import ProviderUnavailableError


def bearer_token(authorization: str) -> str:
    """The token of an ``Authorization: Bearer`` header, or 401 for any other shape."""
    scheme, _, raw = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw:
        raise UnauthenticatedError("malformed bearer")
    return raw


def _claims_issuer(token: str, issuer: str) -> bool:
    """Whether the token claims ``issuer``, read before any signature is checked: the routing question."""
    try:
        claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == issuer.rstrip("/")


def service_issued(request: Request, settings: MedallionSettings, token: str) -> bool:
    """Whether this bearer is a service-account token, so it belongs to `verify_service` and never to Dex.

    Read off the configured issuer as well as the built verifier, so a verifier that failed to build still
    routes its tokens here, where they are refused 503 rather than handed to Dex as a person's.
    """
    verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if verifier is not None:
        return verifier.issued(token)
    return bool(settings.sa_issuer) and _claims_issuer(token, str(settings.sa_issuer))


async def verify_service(request: Request, token: str, *, caller_app_id: str | None, resource: str) -> ServicePrincipal:
    """The service a `service_issued` token proves: 401 for a token at fault, 503 for an unusable issuer.

    A service arriving through a public front door is refused: the front door forwards strangers, so a
    machine principal never legitimately arrives through it.
    """
    verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if verifier is None:
        audit("authn", FAILURE, resource=resource, reason="verifier_unavailable")
        raise ServiceUnavailableError("service-account authentication is configured but unavailable")
    try:
        # Off the loop: a cold key set or a rotated key fetches discovery and JWKS synchronously.
        principal = await run_in_threadpool(verifier.verify, token)
    except UnauthenticatedError:
        audit("authn", FAILURE, resource=resource, reason="invalid_token")
        raise UnauthenticatedError("invalid token") from None
    except ProviderUnavailableError as exc:
        audit("authn", FAILURE, resource=resource, reason="verifier_unavailable")
        raise ServiceUnavailableError("service-account authentication is unavailable") from exc
    if is_public_caller(caller_app_id):
        audit("authn", DENY, subject=principal.sub, resource=resource, reason="public_caller")
        raise PermissionDeniedError(f"{caller_app_id!r} is a public front door, and a service never arrives through one")
    return principal


def refuse_unauthenticatable_door(settings: MedallionSettings) -> None:
    """Raise unless an operator acknowledged that this door runs with nothing to authenticate a caller.

    `RASK_INSECURE_ALLOW_UNAUTHENTICATED` is that acknowledgement (`assert_authentication_configured`): with
    no OIDC and no service-account issuer the door can verify nobody, so admitting is the control being
    absent, and only an operator who said so may run it that way.
    """
    if not settings.insecure_allow_unauthenticated:
        raise PermissionDeniedError(
            "this door can authenticate nobody: set RASK_SA_ISSUER (services) or RASK_OIDC_ENABLED (people), "
            "or RASK_INSECURE_ALLOW_UNAUTHENTICATED to run it open deliberately"
        )


async def require_producer(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> ServicePrincipal | None:
    """A stage runner's operator door: the caller is a service its `RASK_SA_SUBJECTS` names, which is the producer.

    The chart maps the producer's account alone at this door, so the map is what binds it: any other
    account's token, the ingest service's included, verifies and then names nobody here (401). ``None`` is
    the acknowledged-open door, which has no subject to return.
    """
    if getattr(request.app.state, "sa_oidc", None) is None and not settings.sa_issuer:
        refuse_unauthenticatable_door(settings)
        return None
    if not authorization:
        audit("authn", FAILURE, resource=request.url.path, reason="missing_token")
        raise UnauthenticatedError("a service-account bearer is required")
    token = bearer_token(authorization)
    if not service_issued(request, settings, token):
        audit("authn", FAILURE, resource=request.url.path, reason="invalid_token")
        raise UnauthenticatedError("this door admits only the producer's service-account token")
    principal = await verify_service(request, token, caller_app_id=dapr_caller_app_id, resource=request.url.path)
    audit("authn", SUCCESS, subject=principal.sub, resource=request.url.path)
    return principal


ProducerService = Annotated[ServicePrincipal | None, Depends(require_producer)]
