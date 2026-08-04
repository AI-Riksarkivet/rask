"""Authn/authz FastAPI dependencies, built once for any governed service.

`services/annotator/api/security.py` grew the first copy of this. A second service needing the same
three things — verify the bearer, name the subject, check one relation — is the moment to put it
somewhere shared rather than write it twice: the lakehouse policy forms had exactly one duplicated
builder, and when the contract widened both copies broke identically.

A FACTORY, not module-level dependencies, because each service types its own settings object. Call
it once at module scope in the service's own security module and export the results.

The three-outcome checker is the load-bearing part, and the middle case is why it exists:

- FGA **off** → permissive, so a dev/offline stack behaves as it did before authz existed.
- FGA **on** with a client → the real check, which retries and fails closed on outage.
- FGA **on** without a client → **503**. Never permissive here: that turns a broken authorization
  layer into an open one, silently, which is the failure nobody notices until it matters.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.oidc import IDToken, OIDCVerifier


#: The subject used when OIDC is disabled. Named rather than inlined so a grep for who can act
#: without a token returns exactly one place.
ANONYMOUS_SUBJECT = "anon"

# auto_error=False: the 401 is raised here so it renders as problem+json like every other error.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


class FgaChecker(Protocol):
    """The one capability a read endpoint needs from OpenFGA.

    Narrower than the real client on purpose: an endpoint should not be able to write tuples or
    enumerate objects, and a test double should not have to pretend it can.
    """

    async def __call__(self, *, user: str, relation: str, obj: str) -> bool: ...


class AuthDeps:
    """The dependencies one service's routes annotate with."""

    def __init__(self, authenticate: Any, current_subject: Any, get_checker: Any) -> None:
        self.authenticate = authenticate
        self.current_subject = current_subject
        self.get_checker = get_checker


def make_auth_deps(settings_dep: Any) -> AuthDeps:
    """Build the authn/authz dependencies against one service's settings type.

    `settings_dep` is that service's `Annotated[TSettings, Depends(get_settings)]`. Everything below
    reads only `oidc_enabled` / `fga_enabled`, which come from the shared `GovernedAuthSettings`
    mixin, so any service carrying that mixin can use this unchanged.
    """

    def authenticate(request: Request, settings: settings_dep, credentials: _CredentialsDep) -> IDToken | None:  # type: ignore[valid-type]
        """Verify the bearer, returning the parsed token (or `None` when OIDC is off)."""
        if not settings.oidc_enabled:
            return None
        verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
        if verifier is None:
            audit("authn", FAILURE, reason="verifier_unavailable")
            raise ServiceUnavailableError("Authentication is enabled but unavailable")
        if credentials is None or not credentials.credentials:
            audit("authn", FAILURE, reason="missing_token")
            raise UnauthorizedError("Missing bearer token")
        try:
            token = verifier.verify(credentials.credentials)
        except Exception:
            audit("authn", FAILURE, reason="invalid_token")
            raise
        audit("authn", SUCCESS, subject=token.sub)
        return token

    def current_subject(token: Annotated[IDToken | None, Depends(authenticate)]) -> str:
        """The verified principal as the FGA subject id (no `user:` prefix — `fga.check` qualifies it).

        With OIDC off this is `anon`, which keeps a dev stack working. With OIDC on it is the token's
        `sub` and nothing else: there is deliberately no header fallback, because a fallback is what
        turns "verified subject" back into "whatever the caller claimed".
        """
        return token.sub if token is not None else ANONYMOUS_SUBJECT

    def get_checker(request: Request, settings: settings_dep) -> FgaChecker:  # type: ignore[valid-type]
        """Resolve the FGA checker — see this module's docstring for the three outcomes."""
        if not settings.fga_enabled:

            async def _open(*, user: str, relation: str, obj: str) -> bool:  # noqa: ARG001
                return True

            return _open

        client = getattr(request.app.state, "fga", None)
        if client is None:
            audit("authz", FAILURE, reason="client_unavailable")
            raise ServiceUnavailableError("Authorization is enabled but unavailable")

        async def _check(*, user: str, relation: str, obj: str) -> bool:
            return await fga.check(client, user=user, relation=relation, obj=obj)

        return _check

    return AuthDeps(authenticate=authenticate, current_subject=current_subject, get_checker=get_checker)


def raw_bearer(credentials: _CredentialsDep) -> str | None:
    """The raw bearer JWT (scheme-stripped), or `None` — for FORWARDING, not verifying.

    A service reading Lance through the REST catalog must forward the CALLER's bearer rather than a
    service credential: the catalog's own `authorize` checks one relation on one `table:` object and
    injects no row predicate, so a service token answers 200 for a caller with no grant at all. The
    two users diverge, not the rows.
    """
    return credentials.credentials if credentials is not None else None


#: The caller's raw bearer JWT (`None` when absent).
RawBearerToken = Annotated[str | None, Depends(raw_bearer)]

__all__ = [
    "ANONYMOUS_SUBJECT",
    "AuthDeps",
    "FgaChecker",
    "RawBearerToken",
    "make_auth_deps",
    "raw_bearer",
]
