"""A service is the Kubernetes service account its projected token names ([[LH-220]], ADR 0156 C5).

A door verifies the token offline against the cluster issuer's key set (`OIDCVerifier`, carrying the fetch
credential and CA that issuer demands): signature, issuer, the door's audience, expiry, and the lifetime it was
minted with. The issuer signs any lifetime a TokenRequest asks for, and nothing offline tells that a token's pod or
account is gone, so a token minted for longer than the door admits (`max_lifetime`) is refused however much of that
lifetime is left ([[LH-585]]): a deleted account's token is good for at most that long. Any token that verifies is
the principal `kubernetes~<sub>` (`lakehouse.governed.principal`), where the token's `sub` is the account's full
username, `system:serviceaccount:<ns>:<sa>`, the name the API server gives the account, so it names one account.
No list of accounts stands in front of the door: an account in any namespace authenticates, and what it may do is
decided by OpenFGA alone, against the tuples that name that principal. An account with no tuple is authenticated and
refused.

A caller reads its own token from the file the kubelet projects, on every request: the kubelet replaces it at 80%
of a 600 s lifetime (measured: rotated at age 515 s), so a copy kept for the life of the process is refused within
ten minutes.
"""

import logging
from pathlib import Path
from typing import Self

import jwt
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from pydantic import BaseModel, ConfigDict, field_validator

from lakehouse.governed.oidc import OIDCVerifier, claimed_issuer
from lakehouse.governed.principal import CLIENT_IDP_SUFFIX, SERVICE_ACCOUNT_IDP, Principal
from lakehouse.governed.projected_token import EmptyTokenFileError, read_token_file


log = logging.getLogger(__name__)

#: The username prefix the Kubernetes API server gives every service account (`system:serviceaccount:<ns>:<sa>`).
_ACCOUNT_PREFIX = "system:serviceaccount:"


def service_account_principal(account: str) -> Principal:
    """The principal a service account is: `kubernetes~` and its full username. `UnauthenticatedError` for any other username.

    A token the issuer signed for something other than a service account (a node, a user it impersonates) names no
    account, so it proves no principal here. Namespace and account name are DNS labels and never empty.
    """
    namespace, _, name = account.removeprefix(_ACCOUNT_PREFIX).partition(":")
    if not account.startswith(_ACCOUNT_PREFIX) or not namespace or not name or ":" in name:
        raise UnauthenticatedError("Invalid or expired token")
    return Principal(idp_id=SERVICE_ACCOUNT_IDP, subject=account)


def client_principal(idp_id: str, client_id: str) -> Principal:
    """The principal a client of the issuer named `idp_id` signed in for itself is: `<idp_id>-client~<client id>`."""
    return Principal(idp_id=f"{idp_id}{CLIENT_IDP_SUFFIX}", subject=client_id)


class ServicePrincipal(BaseModel):
    """A machine caller: a service account the cluster vouched for, or a client the IdP vouched for (ADR 0181)."""

    model_config = ConfigDict(frozen=True)

    principal: Principal

    @field_validator("principal")
    @classmethod
    def _a_machine(cls, principal: Principal) -> Principal:
        if not principal.is_machine:
            raise ValueError(f"{principal.key!r} is a person, not a machine")
        return principal

    @classmethod
    def for_account(cls, account: str) -> Self:
        """The service account `account` names; `UnauthenticatedError` for a username that names none."""
        return cls(principal=service_account_principal(account))

    @property
    def subject(self) -> str:
        """The principal key, the subject id every door's authorization reads from a verified caller."""
        return self.principal.key


class ServiceAccountVerifier:
    """Verify one door's projected service-account tokens, and name the subject each one is."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        cache_ttl: int,
        leeway: int,
        fetch_token_file: str | None,
        ca_file: str | None,
        max_lifetime: int,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self._max_lifetime = max_lifetime
        self._oidc = OIDCVerifier(self.issuer, audience, cache_ttl, leeway=leeway, fetch_token_file=fetch_token_file, ca_file=ca_file)

    def issued(self, token: str) -> bool:
        """Whether the token claims this issuer: the question that routes it, asked before its signature is checked."""
        try:
            return claimed_issuer(token) == self.issuer
        except jwt.PyJWTError:
            return False

    def refresh(self) -> list[tuple[str, str]]:
        """Fetch the issuer's keys now and report what failed; see `OIDCVerifier.refresh`."""
        return self._oidc.refresh()

    async def refresh_forever(self) -> None:
        """Fetch the issuer's keys again in the background until cancelled; see `OIDCVerifier.refresh_forever`."""
        await self._oidc.refresh_forever()

    def verify(self, token: str) -> ServicePrincipal:
        """The principal the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the issuer."""
        claims = self._oidc.verify(token)
        if (lifetime := claims.exp - claims.iat) > self._max_lifetime:
            # The caller is told what every refused token is told; the operator, which account and by how much.
            log.warning("service_account_token_minted_too_long", extra={"account": claims.sub, "lifetime_seconds": lifetime, "max_seconds": self._max_lifetime})
            raise UnauthenticatedError("Invalid or expired token")
        return ServicePrincipal.for_account(claims.sub)


class IdentityTokenUnavailableError(ServiceUnavailableError):
    """This pod's projected identity token could not be read, so the call is not sent rather than sent without it."""


def read_identity_token(path: str | Path) -> str:
    """This pod's projected identity token, read now."""
    try:
        return read_token_file(path)
    except EmptyTokenFileError as exc:
        raise IdentityTokenUnavailableError(f"the identity token at {path} is empty") from exc
    except (OSError, UnicodeError) as exc:
        raise IdentityTokenUnavailableError(f"the identity token at {path} could not be read") from exc


def identity_bearer(path: str | Path) -> dict[str, str]:
    """The Authorization header carrying this pod's identity token, read now."""
    return {"Authorization": f"Bearer {read_identity_token(path)}"}


__all__ = [
    "IdentityTokenUnavailableError",
    "ServiceAccountVerifier",
    "ServicePrincipal",
    "client_principal",
    "identity_bearer",
    "read_identity_token",
    "service_account_principal",
]
