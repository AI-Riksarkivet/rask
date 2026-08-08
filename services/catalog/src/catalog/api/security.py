"""OIDC authentication dependency.

When OIDC is disabled (the default) this is a no-op and all routes stay open.
When enabled, it requires a valid bearer token on every route it guards and maps
auth failures to ``UnauthenticatedError`` (rendered as RFC 9457 problem+json, 401).

Fail-closed invariant: if OIDC is enabled in settings but the verifier was never
wired onto ``app.state`` (e.g. discovery failed at startup, or a deployment skew),
we raise ``ServiceUnavailableError`` (503) rather than silently letting requests
through. A configured-but-broken auth layer must never degrade to open access.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from catalog.api.dependencies import SettingsDep
from service_kit.governed.audit import FAILURE, SUCCESS, audit
from service_kit.governed.dapr_auth import (
    SecretStoreUnreadable,
    ServiceDoorClosed,
    dedicated_token_from_store,
    is_public_caller,
    service_principal,
)
from service_kit.governed.oidc import IDToken, OIDCVerifier


# auto_error=False: we raise UnauthenticatedError ourselves so 401s are problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    x_lance_service_identity: Annotated[str | None, Header()] = None,
    # The INVOKING Dapr app-id — what separates a service from the PUBLIC front door invoking on a
    # stranger's behalf. See `service_kit.governed.dapr_auth.is_public_caller`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> IDToken | None:
    """Authenticate the request: an OIDC bearer (human/external) OR the SERVICE door.

    THE SERVICE DOOR IS SHUT BY DEFAULT (`service_subjects` empty) and this function then behaves
    exactly as it always has. It exists because a service had NO way to authenticate here: the
    catalog verified OIDC JWTs and nothing else, so every ingest run died at its first activity with
    `catalog refused describe (401): Missing bearer token` — and a JWT expires, so the static token
    the medallion carries for the same purpose is the wrong shape for the problem.

    The mechanism is the one lineage already runs, extracted rather than re-invented, so the estate
    keeps ONE service-to-service auth model and the lessons paid for there (the public-caller
    laundering path, privileged subjects needing their own credential) apply here for free.
    """
    if not settings.oidc_enabled:
        return None

    # Both headers, never the token alone: with `dapr.io/app-token-secret` set the SIDECAR stamps
    # `dapr-api-token` on every request it delivers, so gating on the token would divert a
    # gateway-proxied HUMAN into the service door and 403 them on the missing identity.
    if settings.service_subjects and dapr_api_token is not None and x_lance_service_identity is not None:
        # THE LAUNDERING PATH, refused AT THE SERVICE DOOR — not at the top of this function.
        #
        # The rule is sound and unchanged: a service principal is never something the public front
        # door should be able to mint. The gateway forwards through Dapr service invocation and the
        # callee's daprd stamps a valid `dapr-api-token` on the way in, so an ANONYMOUS public
        # request can arrive already holding the estate's service credential; `x-lance-service-identity`
        # is caller-supplied. The gateway strips both at the edge, and this refuses the door even if
        # one gets through.
        #
        # WHAT CHANGED, and why it had to: this check used to run BEFORE anything else, so it fired
        # on every request carrying `dapr-caller-app-id: gateway` — which is EVERY request the
        # gateway proxies, including a signed-in human holding a perfectly valid OIDC bearer that
        # was never even looked at. Measured on the live cluster: the same token, same endpoint,
        # direct to the catalog answered 200 and answered 403 with the single header added. Every
        # authenticated read and write through `/api/catalog/*` — the only public path to the
        # catalog — was dead, and the error told the caller to "sign in and retry" when they had.
        #
        # Minting a SERVICE principal and verifying a HUMAN's bearer are different operations. The
        # bearer is verified cryptographically against the IdP; the gateway cannot forge one, and a
        # caller presenting a valid token is simply being themselves. Refusing that bought no
        # security and cost the entire authenticated surface. Scoping the refusal to this branch
        # keeps the laundering path shut — a public caller cannot reach `service_principal` — while
        # a proxied human falls through to OIDC below, which is the case the estate is FOR.
        if is_public_caller(dapr_caller_app_id):
            audit("authn", FAILURE, reason="public_caller")
            raise PermissionDeniedError(
                f"{dapr_caller_app_id!r} is a public front door: the service door authenticates a service, not a caller — sign in and retry"
            )
        try:
            principal = service_principal(
                token=dapr_api_token,
                identity=x_lance_service_identity,
                allowed_subjects=settings.service_subjects,
                privileged_subjects=settings.privileged_subjects,
                # The shared resolver (open_dapr.md §2.8): without it, this door's privileged branch
                # could never open — every privileged subject was hard-refused with "no dedicated
                # credential provisioned" regardless of what the store held, because the callback
                # defaulted to None.
                dedicated_token=dedicated_token_from_store(settings.dapr_secret_store, settings.dapr_secret_key),
            )
        except ServiceDoorClosed:
            # No APP_API_TOKEN here: nothing to verify, so fall through to OIDC rather than admitting
            # a caller who merely named a subject.
            pass
        except SecretStoreUnreadable as exc:
            # An outage is an outage, never a 401: the absent-vs-unreadable rule (§2.17).
            raise ServiceUnavailableError(str(exc)) from exc
        else:
            audit("authn", SUCCESS, subject=principal.sub)
            # A SYNTHETIC token, and every field is deliberate. `IDToken` requires the conformant
            # OIDC core claims (iss/sub/aud/exp/iat) — supplying only `sub` raised a pydantic
            # ValidationError that surfaced to the caller as a bare catalog 500, which reads as a
            # broken catalog rather than a malformed principal.
            #
            # `iss` names the SERVICE DOOR rather than the IdP, so an audit line can never be
            # mistaken for a human login; `exp` is short because nothing re-verifies this object —
            # it is already authenticated and exists only to carry `sub` into the authz layer, where
            # a service is bounded by its own FGA rung exactly like a person.
            now = int(time.time())
            return IDToken(
                iss="rask://service-door",
                sub=principal.sub,
                aud=settings.oidc_audience or "rask",
                iat=now,
                exp=now + 60,
                service=True,
            )

    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # OIDC is enabled but no verifier is available: fail closed, never open.
        audit("authn", FAILURE, reason="verifier_unavailable")
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if credentials is None or not credentials.credentials:
        audit("authn", FAILURE, reason="missing_token")
        raise UnauthenticatedError("Missing bearer token")
    try:
        token = verifier.verify(credentials.credentials)
    except Exception:
        audit("authn", FAILURE, reason="invalid_token")
        raise
    audit("authn", SUCCESS, subject=token.sub)  # #41 record the authenticated principal
    return token


#: Token of the authenticated caller (``None`` when OIDC is disabled). Endpoints that
#: need claims can depend on this; router-level use enforces authentication.
CurrentToken = Annotated[IDToken | None, Depends(authenticate)]


def raw_bearer(credentials: _CredentialsDep) -> str | None:
    """The raw bearer JWT string (scheme-stripped), or ``None`` when no bearer is present.

    For routes that must FORWARD the caller's token rather than only verify it — e.g. credential vending's
    web_identity flow re-presents it to the object store (AssumeRoleWithWebIdentity). Reuses the single
    ``HTTPBearer`` seam, so parsing matches :func:`authenticate` (case-insensitive scheme — ``BEARER …`` too).
    """
    return credentials.credentials if credentials is not None else None


#: The caller's raw bearer JWT (``None`` when absent) — for forwarding, not verification.
RawBearerToken = Annotated[str | None, Depends(raw_bearer)]
