"""OIDC authentication + the FGA checker seam for the annotator.

Mirrors `services/catalog/api/security.py` deliberately — same bearer parsing, same fail-closed
posture, same audit calls — so the two governed services cannot drift into two different answers to
"who is this request".

**Fail-closed twice.** If OIDC is enabled but no verifier reached `app.state` (discovery failed at
startup, or a deployment skew), we raise 503 rather than let requests through; likewise if FGA is
enabled but no client was built. A configured-but-broken auth layer must never degrade to open
access — that is the failure mode the whole governed plane exists to prevent.

**Why the annotator needs this at all**: it writes per-subject state (projects, tasks, claims,
drafts). `service_kit.media.deps.get_author` reads a *trusted* `X-User` header defaulting to
`"anon"`, which is fine for a read-plane media service and a cross-user leak for anything keyed on
identity.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from annotator.core.config import AnnotatorSettings, get_annotator_settings
from service_kit.exceptions import ServiceUnavailableError, UnauthorizedError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.oidc import IDToken, OIDCVerifier


# auto_error=False: we raise UnauthenticatedError ourselves so 401s render as problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]

SettingsDep = Annotated[AnnotatorSettings, Depends(get_annotator_settings)]

#: The subject used when OIDC is disabled. Named rather than inlined so a grep for who can act
#: without a token returns exactly one place.
ANONYMOUS_SUBJECT = "anon"


def authenticate(request: Request, settings: SettingsDep, credentials: _CredentialsDep) -> IDToken | None:
    """Authenticate the request, returning the parsed token (or ``None`` when OIDC is off)."""
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


CurrentToken = Annotated[IDToken | None, Depends(authenticate)]


def current_subject(token: CurrentToken) -> str:
    """The verified principal, as the FGA subject id (no ``user:`` prefix — `fga.check` qualifies it).

    With OIDC off this is `anon`, which matches the pre-auth behaviour of `get_author` so a dev stack
    keeps working. With OIDC on it is the token's `sub` and nothing else: there is deliberately no
    header fallback, because a fallback is what turns "verified subject" back into "whatever the
    caller claimed".
    """
    return token.sub if token is not None else ANONYMOUS_SUBJECT


CurrentSubject = Annotated[str, Depends(current_subject)]


class FgaChecker(Protocol):
    """The one capability the annotation-project routes need from OpenFGA.

    Narrower than the real client on purpose: an endpoint should not be able to write tuples or
    enumerate objects, and a test double should not have to pretend it can.
    """

    async def __call__(self, *, user: str, relation: str, obj: str) -> bool: ...


def get_checker(request: Request, settings: SettingsDep) -> FgaChecker:
    """Resolve the FGA checker.

    Three outcomes, and the middle one is the point:

    - FGA **off** → a permissive checker, so a dev/offline stack behaves as it did before authz.
    - FGA **on** with a client → the real `fga.check`, which retries and fails closed on outage.
    - FGA **on** without a client → **503**. Never fall back to permissive here: that would turn a
      broken authz layer into an open one, silently.
    """
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


CheckerDep = Annotated[FgaChecker, Depends(get_checker)]
