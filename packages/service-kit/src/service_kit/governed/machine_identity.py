"""A service is the Kubernetes service account its projected token names ([[LH-220]], ADR 0156 C5).

A door verifies the token offline against the cluster issuer's key set (`OIDCVerifier`, carrying the fetch
credential and CA that issuer demands): signature, issuer, the door's audience and expiry. Any token that verifies is
the principal `kubernetes~<sub>` (`service_kit.governed.principal`), where the token's `sub` is the account's full
username, `system:serviceaccount:<ns>:<sa>` (Lakekeeper's `username` subject source, crates/lakekeeper/src/config.rs).
No list of accounts stands in front of the door: an account in any namespace authenticates, and what it may do is
decided by OpenFGA alone, against the tuples that name that principal. An account with no tuple is authenticated and
refused.

A caller reads its own token from the file the kubelet projects, on every request: the kubelet replaces it at 80%
of a 600 s lifetime (measured: rotated at age 515 s), so a copy kept for the life of the process is refused within
ten minutes.
"""

from __future__ import annotations

from pathlib import Path

import jwt
from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict, field_validator

from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.oidc import OIDCVerifier
from service_kit.governed.principal import CLIENT_IDP, SERVICE_ACCOUNT_IDP, Principal


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


def client_principal(client_id: str) -> Principal:
    """The principal a Dex client signed in for itself is: `dex-client~` and its client id (ADR 0181)."""
    return Principal(idp_id=CLIENT_IDP, subject=client_id)


class ServicePrincipal(BaseModel):
    """A machine caller: a service account the cluster vouched for, or a Dex client the IdP vouched for (ADR 0181)."""

    model_config = ConfigDict(frozen=True)

    principal: Principal

    @field_validator("principal")
    @classmethod
    def _a_machine(cls, principal: Principal) -> Principal:
        if not principal.is_machine:
            raise ValueError(f"{principal.key!r} is a person, not a machine")
        return principal

    @classmethod
    def for_account(cls, account: str) -> ServicePrincipal:
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
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self._oidc = OIDCVerifier(self.issuer, audience, cache_ttl, leeway=leeway, fetch_token_file=fetch_token_file, ca_file=ca_file)

    def issued(self, token: str) -> bool:
        """Whether the token claims this issuer: the question that routes it, asked before its signature is checked."""
        try:
            claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
        except jwt.PyJWTError:
            return False
        return isinstance(claimed, str) and claimed.rstrip("/") == self.issuer

    def warm(self) -> list[tuple[str, str]]:
        """Resolve the issuer now and report what failed; see `OIDCVerifier.warm`."""
        return self._oidc.warm()

    def verify(self, token: str) -> ServicePrincipal:
        """The principal the token proves; `UnauthenticatedError` for a token at fault, `ProviderUnavailableError` for the issuer."""
        return ServicePrincipal.for_account(self._oidc.verify(token).sub)


class IdentityTokenUnavailableError(ServiceUnavailableError):
    """This pod's projected identity token could not be read, so the call is not sent rather than sent without it."""


def read_identity_token(path: str | Path) -> str:
    """This pod's projected identity token, read now."""
    try:
        token = Path(path).read_text().strip()
    except OSError as exc:
        raise IdentityTokenUnavailableError(f"the identity token at {path} could not be read") from exc
    if not token:
        raise IdentityTokenUnavailableError(f"the identity token at {path} is empty")
    return token


def identity_bearer(path: str | Path) -> dict[str, str]:
    """The Authorization header carrying this pod's identity token, read now."""
    return {"Authorization": f"Bearer {read_identity_token(path)}"}
