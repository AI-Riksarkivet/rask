"""The caller a bearer proves at a governed door, and the record each answer leaves ([[LH-220]], [[LH-585]]).

A door admits two kinds of bearer: a service's projected service-account token, verified against the cluster issuer,
and a person's or a client's token from an identity provider. Which verifier judges a bearer is decided by its
unverified ``iss``, the service-account issuer first; the verifier then checks the signature, audience and expiry.

Every outcome writes one ``authn`` audit record: who was admitted, or why a bearer was refused (``missing_token``,
``invalid_token``, or ``verifier_unavailable`` when the fault is the deployment's and not the caller's). The catalog
and lineage both call this, so no door admits or refuses a caller without the record another door would have left.
"""

from typing import Protocol

import jwt
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from starlette.datastructures import State

from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.idp_identity import IdpVerifier, PersonPrincipal
from lakehouse.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from lakehouse.governed.oidc import ProviderUnavailableError


class DoorSettings(Protocol):
    """What a door's settings say about who may knock."""

    @property
    def oidc_enabled(self) -> bool: ...
    @property
    def sa_issuer(self) -> str | None: ...


def caller_of(state: State, settings: DoorSettings, bearer: str | None) -> PersonPrincipal | ServicePrincipal | None:
    """The caller ``bearer`` proves at the door whose app holds ``state``; ``None`` with OIDC off.

    Raises:
        UnauthenticatedError: No bearer was presented, or the bearer proves nobody.
        ServiceUnavailableError: A verifier is missing or cannot reach or trust its issuer: the deployment's fault, never
            recorded against the caller.
    """
    if not settings.oidc_enabled:
        return None
    sa_verifier: ServiceAccountVerifier | None = getattr(state, "sa_oidc", None)
    if bearer and _claims_the_service_account_issuer(bearer, settings, sa_verifier):
        return _proved(sa_verifier, bearer)
    verifier: IdpVerifier | None = getattr(state, "oidc", None)
    if verifier is not None and not bearer:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="missing_token")
        raise UnauthenticatedError("Missing bearer token")
    return _proved(verifier, bearer or "")


def _claims_the_service_account_issuer(bearer: str, settings: DoorSettings, verifier: ServiceAccountVerifier | None) -> bool:
    """Whether the bearer's unverified ``iss`` is the service-account issuer: the routing question, asked before any signature check.

    With no verifier built the configured issuer still answers it, so a service-account token is refused as an outage
    rather than handed to the identity provider's verifier.
    """
    if verifier is not None:
        return verifier.issued(bearer)
    if not settings.sa_issuer:
        return False
    try:
        claimed = jwt.decode(bearer, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return False
    return isinstance(claimed, str) and claimed.rstrip("/") == settings.sa_issuer.rstrip("/")


def _proved(verifier: IdpVerifier | ServiceAccountVerifier | None, bearer: str) -> PersonPrincipal | ServicePrincipal:
    """The principal ``verifier`` reads off ``bearer``, each outcome recorded once."""
    if verifier is None:
        # Enabled with no verifier built (a failed boot): fail closed, and keep the broken boot visible.
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    try:
        principal = verifier.verify(bearer)
    except ProviderUnavailableError as exc:
        # The issuer, or this deployment's view of it, failed. Recorded as the caller's, it would write one false
        # refusal per request for as long as the outage lasts.
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable") from exc
    except Exception:
        audit(AuditAction.AUTHN, AuditOutcome.FAILURE, reason="invalid_token")
        raise
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject=principal.subject)
    return principal


__all__ = [
    "DoorSettings",
    "caller_of",
]
