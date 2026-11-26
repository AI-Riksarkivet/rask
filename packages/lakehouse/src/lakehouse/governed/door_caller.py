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
from lakehouse.governed.oidc import ProviderUnavailableError, claimed_issuer
from lakehouse.governed.settings import OidcSettings
from lakehouse.naming import ANONYMOUS


class NamedCaller(Protocol):
    """A verified caller, as far as naming it goes: a person's or a machine's principal, each named by its key."""

    @property
    def subject(self) -> str: ...


def caller_subject(caller: NamedCaller | None) -> str:
    """The subject a door's audit record, a tuple it writes and a refusal it answers name its caller by: the principal key
    :func:`caller_of` verified, or `lakehouse.naming.ANONYMOUS` where no caller was verified (authentication off)."""
    return caller.subject if caller is not None else ANONYMOUS


def caller_of(state: State, settings: OidcSettings, bearer: str | None) -> PersonPrincipal | ServicePrincipal | None:
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


def _claims_the_service_account_issuer(bearer: str, settings: OidcSettings, verifier: ServiceAccountVerifier | None) -> bool:
    """Whether the bearer's unverified ``iss`` is the service-account issuer: the routing question, asked before any signature check.

    With no verifier built the configured issuer still answers it, so a service-account token is refused as an outage
    rather than handed to the identity provider's verifier.
    """
    if verifier is not None:
        return verifier.issued(bearer)
    if not settings.sa_issuer:
        return False
    try:
        return claimed_issuer(bearer) == settings.sa_issuer.rstrip("/")
    except jwt.PyJWTError:
        return False


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
    "NamedCaller",
    "caller_of",
    "caller_subject",
]
