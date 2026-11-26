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

from typing import Annotated, overload

from fastapi import Depends

from catalog.api.dependencies import request_settings
from lakehouse.governed.admission import Caller, door_gate
from lakehouse.governed.door_caller import caller_subject
from lakehouse.naming import ANONYMOUS


#: The catalog's gate: every door depending on `authenticate` judges its caller from the headers before its body is
#: read (`lakehouse.governed.admission`), so no caller without a token makes this service buffer one. Every account in
#: the cluster whose token carries this door's audience authenticates, as its own subject, and is authorized by its
#: tuples alone.
_GATE = door_gate(request_settings)
authenticate = _GATE.authenticate
judge = _GATE.judge

#: The authenticated caller (``None`` when OIDC is disabled). Endpoints that need claims depend on
#: this and narrow with ``isinstance``; router-level use enforces authentication.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]


@overload
def actor_of(token: Caller) -> str: ...
@overload
def actor_of(token: Caller | None) -> str | None: ...
def actor_of(token: Caller | None) -> str | None:
    """The verified caller as the actor an event or a record names, or ``None`` with OIDC off.

    The OpenFGA user its principal is (`Principal.fga_user`: `user:` and the key, percent-encoded), which every tuple
    naming the caller holds, so a client joining an actor to the grants finds the same person ([[LH-608]]). The audit
    trail and a tuple's writer name the caller by its subject (`lakehouse.governed.door_caller.caller_subject`).
    """
    return token.principal.fga_user if token is not None else None


def record_actor_of(token: Caller | None) -> str:
    """The verified caller as a record that always says who acted names it: :func:`actor_of`, or ``anonymous`` with
    OIDC off (`lakehouse.naming.ANONYMOUS`), as a trash entry and a deletion protection do."""
    return actor_of(token) or ANONYMOUS


async def current_subject(token: CurrentToken) -> str:
    """The request's caller as its subject (`lakehouse.governed.door_caller.caller_subject`), as a dependency."""
    return caller_subject(token)


CurrentSubject = Annotated[str, Depends(current_subject)]
