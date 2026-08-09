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

from typing import Annotated, Protocol

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from lineage.api.dependencies import SettingsDep
from service_kit.governed.dapr_auth import (
    CredentialRejected,
    SecretStoreUnreadable,
    ServiceDoorClosed,
    SubjectNotAllowed,
    dedicated_token_from_store,
    is_public_caller,
    service_principal,
)
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


def _service_principal(settings: SettingsDep, token: str | None, identity: str | None) -> ServicePrincipal:
    """Render the estate's ONE service door in lineage's problem vocabulary. It decides nothing.

    THE FORK IS GONE (open_dapr.md §2.8). This function used to be a full second copy of
    `service_kit.governed.dapr_auth.service_principal` — same two questions, same allowlist, same
    privileged branch, and a DIFFERENT answer when `APP_API_TOKEN` was unset: it refused where the
    shared door signalled fall-through. Two doors that disagree is worse than either answer, because
    whichever one an auditor reads, the other is the one that ran. The unified answer is the refusal
    (`ServiceDoorClosed`'s docstring carries the reasoning); the mechanism now lives in one body.

    The TWO questions it delegates, restated because they are what the door is FOR:

      1. may this SUBJECT use the service door at all?  ->  `service_subjects`, the allowlist
      2. may THIS CALLER be that subject?               ->  the credential

    Without (2) the identity is a claim the door BELIEVES. With one shared app token and an allowlist
    of `service-trainer,service-web`, any holder of that token could pick either — and they are not
    peers: `service-web` is a reader on the warehouse, `service-trainer` is `writer` on
    `namespace:models`. That shared token lives in the env of seven sidecar-less web pods, so env
    read in any one of them meant forged, author-stamped writes into the authoritative lineage graph.
    A privileged subject therefore needs its OWN credential, and the shared token cannot claim it.

    The store coordinates come from SETTINGS, not from `os.environ`. The fork read
    `LINEAGE_SECRET_STORE`/`LINEAGE_SECRET_KEY`, names that nothing in the estate sets — the chart, the
    compose stacks and `apply_dapr_secrets` all speak `LINEAGE_DAPR_SECRET_STORE`/`_KEY` — so an
    operator who repointed the store correctly still had this door querying `lance-secrets`, getting
    `{}` back, and refusing every privileged subject on a deployment that looked configured.
    """
    try:
        principal = service_principal(
            token=token,
            identity=identity,
            allowed_subjects=settings.service_subjects,
            privileged_subjects=settings.privileged_subjects,
            # Deferred into the callback: the store is read only when a privileged subject is actually
            # being verified, never on the shared-token path — and never for an unlisted subject, which
            # is checked first so this door is not an enumeration oracle.
            dedicated_token=dedicated_token_from_store(settings.dapr_secret_store, settings.dapr_secret_key),
        )
    except ServiceDoorClosed as exc:
        # The caller asked for the service door by sending both service headers, and it does not exist
        # in this deployment. Say so — a fall-through would answer them with "Missing bearer token".
        raise UnauthenticatedError(str(exc)) from exc
    except SubjectNotAllowed as exc:
        # Fail closed on an unlisted subject — this is what stops a token holder impersonating a human.
        raise PermissionDeniedError(str(exc)) from exc
    except CredentialRejected as exc:
        # Includes the privileged subject whose credential was never provisioned: it must NOT fall back
        # to the shared token — that is the escalation this exists to stop, and a quiet fallback would
        # restore it while looking configured.
        raise UnauthenticatedError(str(exc)) from exc
    except SecretStoreUnreadable as exc:
        # An outage is an outage, never a 401: the absent-vs-unreadable rule (open_dapr.md §2.17).
        raise ServiceUnavailableError(str(exc)) from exc
    return ServicePrincipal(principal.sub)


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
    #
    # BOTH headers, on the other hand, is the caller ASKING for the service door, so the door is the
    # only one they get: a refusal inside this branch is final and never re-asks OIDC. The catalog
    # answers identically (open_dapr.md §2.8) — that agreement is the point.
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
