"""A service is the Kubernetes service account its projected token names ([[LH-220]], D1).

A door verifies the token offline against the cluster issuer's key set (`OIDCVerifier`, carrying the fetch
credential and CA that issuer demands), then maps the token's full username, `system:serviceaccount:<ns>:<sa>`,
to the subject the estate's grants name. The map is exact and keyed on the full username, so the same account
name in another namespace is another key. A verified token whose username the map does not name is refused, never
handed to another verifier: signature and audience alone accept every account in the cluster that carries the
audience (measured 2026-10-02, the P5.3 c0 probe).

A caller reads its own token from the file the kubelet projects, on every request: the kubelet replaces it at 80%
of a 600 s lifetime (measured: rotated at age 515 s), so a copy kept for the life of the process is refused within
ten minutes.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import jwt
from lance_namespace import UnauthenticatedError
from pydantic import BaseModel, ConfigDict

from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.oidc import OIDCVerifier


class ServicePrincipal(BaseModel):
    """A caller the cluster vouched for: the subject its account maps to, and the account itself."""

    model_config = ConfigDict(frozen=True)

    subject: str
    service_account: str

    @property
    def sub(self) -> str:
        """The subject, under the name every door's authorization reads from a verified caller."""
        return self.subject


class ServiceAccountVerifier:
    """Verify one door's projected service-account tokens, and name the subject each one maps to."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        subjects: Mapping[str, str],
        *,
        cache_ttl: int,
        leeway: int,
        fetch_token_file: str | None,
        ca_file: str | None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self._subjects = dict(subjects)
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
        account = self._oidc.verify(token).sub
        subject = self._subjects.get(account)
        if subject is None:
            raise UnauthenticatedError("Invalid or expired token")
        return ServicePrincipal(subject=subject, service_account=account)


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
