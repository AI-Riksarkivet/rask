"""OIDC authentication dependency for the lineage read + ingest endpoints.

When OIDC is disabled (the default) this is a no-op and all routes stay open. When
enabled, it requires a verified bearer token and maps auth failures to RFC 9457
problem+json (401). It binds to ``LineageSettings`` but otherwise mirrors the catalog's
``services/catalog/api/security.py`` and reuses the catalog's :class:`~service_kit.governed.oidc.OIDCVerifier` — so
token verification has one source of truth.

Fail-closed invariant: if OIDC is enabled in settings but the verifier was never wired
onto ``app.state`` (startup/discovery skew), we raise ``ServiceUnavailableError`` (503)
rather than silently letting requests through.
"""

from __future__ import annotations

import os
import secrets as _secrets
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from lineage.api.dependencies import SettingsDep
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.oidc import IDToken, OIDCVerifier


# auto_error=False: we raise UnauthenticatedError ourselves so 401s render as problem+json.
_bearer = HTTPBearer(auto_error=False, description="OIDC bearer token")
_CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


class Principal(Protocol):
    """What the ingest authz layer actually needs of a caller: a subject to attribute + authorize.

    Both :func:`~lineage.api.fga_deps.enforce_author` and
    :func:`~lineage.api.fga_deps.enforce_output_authz` read only ``.sub``, so an OIDC ``IDToken``
    and a :class:`ServicePrincipal` are interchangeable there.
    """

    @property
    def sub(self) -> str: ...


class ServicePrincipal:
    """A non-human, in-cluster producer authenticated by the shared app token (NOT OIDC).

    **Why this exists.** Services in this system are not OIDC identities: the movers/catalog/compaction
    reach lineage over the Dapr subscription route, which is guarded by the app-api-token and attributes
    the run to the producer-stamped author (``lineage/api/dapr.py``). OIDC is the *human/external* door.
    The Ray TRAIN job is an internal producer that simply has no sidecar (docs/RAY-TRAIN.md D2), so it
    POSTs the HTTP ingest — where, before this, it hit the OIDC wall and **every training RunEvent 401'd,
    silently losing all training provenance in a governed deployment** (live 2026-07-13).

    Minting a Dex user for it would introduce exactly the "second identity axis the lineage graph must
    bridge" that D3 argues against. So a service authenticates with the app token it already shares, and
    NAMES itself with the same bare FGA subject the rest of the estate uses (``service-trainer``) — the
    identity D5 already grants (``writer`` on ``namespace:models`` only).

    The claimed subject is **allowlisted** (``LINEAGE_SERVICE_SUBJECTS``): a token holder can only speak
    as a configured service, never as a human — so this cannot become an impersonation primitive. And it
    is *stricter* than the Dapr route it mirrors: the outputs are still FGA-checked as that subject, so a
    trainer can only record provenance for what its rung lets it write.
    """

    def __init__(self, subject: str) -> None:
        self._sub = subject

    @property
    def sub(self) -> str:
        return self._sub


def _dedicated_token(identity: str) -> str | None:
    """The credential that is the ONLY one able to claim `identity`, via the estate's ONE resolver.

    The resolution itself lives in ``service_kit.governed.dapr_auth.dedicated_token_from_store``
    (open_dapr.md §2.8 — this used to be a private fork here while the catalog's door had no
    resolver at all; §2.17's caching + retries=1 + absent-vs-unreadable split live there now).
    This wrapper only maps the neutral unreadable-store error onto lineage's problem type. Returns
    None only when the bundle was READ and the field is genuinely absent. Deliberately no env
    fallback: a credential readable from process env is the shape the estate's rule forbids, and
    it is exactly how the shared token came to sit in seven web pods.
    """
    from service_kit.governed.dapr_auth import SecretStoreUnreadable, dedicated_token_from_store

    store = os.environ.get("LINEAGE_SECRET_STORE", "lance-secrets")
    key = os.environ.get("LINEAGE_SECRET_KEY", "lance")
    try:
        return dedicated_token_from_store(store, key)(identity)
    except SecretStoreUnreadable as exc:
        raise ServiceUnavailableError(str(exc)) from exc


def _service_principal(settings: SettingsDep, token: str | None, identity: str | None) -> ServicePrincipal:
    """Authenticate an in-cluster service producer: a valid credential + an ALLOWLISTED subject.

    TWO questions, and this door used to answer only the first:

      1. may this SUBJECT use the service door at all?  ->  `service_subjects`, the allowlist
      2. may THIS CALLER be that subject?               ->  the credential, below

    Without (2) the identity is a claim the door BELIEVES. With one shared app token and an allowlist
    of `service-trainer,service-web`, any holder of that token could pick either — and they are not
    peers: `service-web` is a reader on the warehouse, `service-trainer` is `writer` on
    `namespace:models`. That shared token lives in the env of seven sidecar-less web pods, so env
    read in any one of them meant forged, author-stamped writes into the authoritative lineage graph.

    A privileged subject therefore needs its OWN credential, and the shared token cannot claim it.
    """
    expected = os.environ.get("APP_API_TOKEN")
    if not expected:
        # The service door only exists when the app token does. Without it there is nothing to verify,
        # so an unauthenticated caller must not be able to open it by merely naming a subject.
        raise UnauthenticatedError("service ingest is not configured")

    allowed = {s.strip() for s in settings.service_subjects.split(",") if s.strip()}
    if not identity or identity not in allowed:
        # Fail closed on an unlisted subject — this is what stops a token holder impersonating a human.
        raise PermissionDeniedError(f"service identity not allowed: {identity or '<missing>'}")

    privileged = {s.strip() for s in settings.privileged_subjects.split(",") if s.strip()}
    if identity in privileged:
        # FAIL CLOSED on a missing secret. A privileged subject whose credential is unprovisioned must
        # NOT fall back to the shared token — that is the escalation this exists to stop, and a quiet
        # fallback would restore it while looking configured.
        dedicated = _dedicated_token(identity)
        if not dedicated:
            raise UnauthenticatedError(f"service identity {identity!r} is privileged but has no dedicated credential provisioned")
        if not _secrets.compare_digest((token or "").encode(), dedicated.encode()):
            raise UnauthenticatedError(f"the presented credential may not claim {identity!r}")
        return ServicePrincipal(identity)

    if not _secrets.compare_digest((token or "").encode(), expected.encode()):
        raise UnauthenticatedError("invalid service token")
    return ServicePrincipal(identity)


def authenticate(
    request: Request,
    settings: SettingsDep,
    credentials: _CredentialsDep,
    dapr_api_token: Annotated[str | None, Header()] = None,
    x_lance_service_identity: Annotated[str | None, Header()] = None,
    # The INVOKING Dapr app-id — what separates a service from the public front door invoking on a
    # stranger's behalf. See `service_kit.governed.dapr_auth.is_public_caller`.
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Authenticate the caller: an OIDC bearer (human/external) OR the service door (in-cluster producer).

    Returns ``None`` when OIDC is off — the open dev default, unchanged.
    """
    if not settings.oidc_enabled:
        return None
    # The service door opens only on a DELIBERATE service call: both the token and the identity header.
    # Gating on the token alone breaks gateway-proxied humans — with dapr.io/app-token-secret set, the
    # SIDECAR stamps `dapr-api-token` on every request it delivers (its app-authentication feature), so a
    # service-invoked request carrying a valid user bearer would be diverted into the service door and
    # 403 on the missing identity (audit 2026-07-15). A token-only request now falls through to OIDC,
    # which still requires a valid bearer — the door itself stays exactly as strict (app token + allowlist).
    if dapr_api_token is not None and x_lance_service_identity is not None:
        # THE LAUNDERING PATH, refused AT THE SERVICE DOOR. The gateway forwards through Dapr service
        # invocation and the callee's daprd stamps a valid `dapr-api-token` on the way in, so an
        # ANONYMOUS public request arrives here already holding the estate's service credential — and
        # `x-lance-service-identity` is caller-supplied, so it can name an allowlisted subject itself.
        # Together that is a forged, author-stamped write into the authoritative lineage graph. The
        # gateway strips both headers at the edge; this refuses the door even if one ever gets through,
        # because a service principal is never something the public front door should be able to mint.
        #
        # It sits INSIDE this branch, not above it. Raising unconditionally re-created — more broadly —
        # the very bug the comment above already warns about: every request the gateway proxies carries
        # `dapr-caller-app-id: gateway`, so a signed-in human was refused before their valid bearer was
        # ever examined, and told to "sign in and retry" when they had. Verifying a human's IdP-signed
        # bearer is not minting a service principal, and only the latter is the laundering risk. The
        # catalog carried the identical defect; both are fixed together.
        if is_public_caller(dapr_caller_app_id):
            raise PermissionDeniedError(
                f"{dapr_caller_app_id!r} is a public front door: the service door authenticates a service, not a caller — sign in and retry"
            )
        return _service_principal(settings, dapr_api_token, x_lance_service_identity)
    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if verifier is None:
        # Enabled but no verifier wired (startup/discovery skew): fail closed, never open.
        raise ServiceUnavailableError("Authentication is enabled but unavailable")
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError("Missing bearer token")
    return verifier.verify(credentials.credentials)


#: The authenticated caller — an OIDC ``IDToken``, a ``ServicePrincipal``, or ``None`` when OIDC is off.
CurrentToken = Annotated[Principal | None, Depends(authenticate)]

__all__ = ["CurrentToken", "IDToken", "Principal", "ServicePrincipal", "authenticate"]
