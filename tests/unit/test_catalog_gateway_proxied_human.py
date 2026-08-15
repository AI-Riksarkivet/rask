"""A signed-in human proxied by the gateway must authenticate AS THE HUMAN.

`/api/catalog/*` is the ONLY public path to the catalog, and every request the gateway proxies
carries `dapr-caller-app-id: gateway`. `catalog.api.security.authenticate` used to refuse on that
header BEFORE looking at the bearer, so every authenticated read and write through the public path
403'd — and the message told the caller to "sign in and retry" when they already had.

Measured on the live cluster 2026-08-06, isolated to the single header (direct to svc/rask-catalog,
same valid token both times):

    valid bearer, no dapr-caller-app-id          -> 200
    valid bearer + dapr-caller-app-id: gateway   -> 403

The gap that let it ship: every existing test calls `authenticate` with no `dapr_caller_app_id`, so
the proxied shape — the ONLY shape a real user ever produces — was never exercised. These tests fix
that, and they fail on the pre-fix code.

The security rule is unchanged and pinned below: a public front door still cannot MINT a service
principal. Minting a service identity and verifying a human's IdP-signed bearer are different
operations, and only the first is the laundering risk.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api import security
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError, UnauthenticatedError

from service_kit.governed.oidc import IDToken


_GATEWAY = "gateway"
_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"


def _token(sub: str = _SUB) -> IDToken:
    return IDToken(iss="https://dex.example/dex", sub=sub, aud="lance-catalog", iat=0, exp=1 << 31)


def _request(*, oidc: object | None = None) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(oidc=oidc))
    return cast(Request, SimpleNamespace(app=app))


def _creds() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.real.jwt")


def _settings(**over: Any) -> Any:
    """Structural stand-in for `catalog.core.config.Settings` — every field `authenticate` reads.

    THE STORE COORDINATES ARE NOT OPTIONAL HERE. Since §2.8 unified the two call sites, the resolver
    is BUILT before the door is called (`dedicated_token_from_store(settings.dapr_secret_store,
    settings.dapr_secret_key)` is an argument expression), so both fields are read on every request
    that carries both service headers — not only on the privileged path. Omitting them made an
    under-specified double raise `AttributeError` where the door should have answered, which is a
    test-harness failure wearing a security test's name.

    The subject lists are `str`, matching the real `Settings` fields (`config.py:206`, `:209`). They
    were tuples, which survived only because no test reached the door's `.split()` — one leaked
    `APP_API_TOKEN` in the environment and the same `AttributeError` would have appeared there.
    """
    base: dict[str, Any] = {
        "oidc_enabled": True,
        "oidc_audience": "lance-catalog",
        "service_subjects": "",
        "privileged_subjects": "",
        "dapr_secret_store": "lance-secrets",
        "dapr_secret_key": "lance",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _verifier() -> object:
    return SimpleNamespace(verify=lambda _t: _token())


# --------------------------------------------------------------------------- #
# THE REGRESSION — a proxied human authenticates as the human
# --------------------------------------------------------------------------- #


def test_gateway_proxied_human_with_a_valid_bearer_authenticates() -> None:
    """The whole point. Fails on the pre-fix code with PermissionDeniedError."""
    token = security.authenticate(
        _request(oidc=_verifier()),
        _settings(),
        _creds(),
        dapr_caller_app_id=_GATEWAY,
    )
    assert token is not None
    assert token.sub == _SUB
    # A HUMAN, not a synthetic service principal: the service door stamps `iss="rask://service-door"`
    # and sets a `service` extra, so the real IdP issuer proves which branch answered.
    assert token.iss == "https://dex.example/dex"


def test_the_sidecar_stamped_token_alone_does_not_divert_a_human() -> None:
    """With `dapr.io/app-token-secret` set, daprd stamps `dapr-api-token` on EVERY delivered request.

    A proxied human therefore arrives holding it. They must still authenticate as themselves — the
    service door needs BOTH headers, and the gateway strips `x-lance-service-identity` at the edge.
    """
    token = security.authenticate(
        _request(oidc=_verifier()),
        _settings(service_subjects="medallion"),
        _creds(),
        dapr_api_token="the-estate-service-token",
        x_lance_service_identity=None,
        dapr_caller_app_id=_GATEWAY,
    )
    assert token is not None and token.sub == _SUB


# --------------------------------------------------------------------------- #
# THE RULE THAT MUST NOT HAVE LOOSENED
# --------------------------------------------------------------------------- #


def test_public_front_door_still_cannot_mint_a_service_principal() -> None:
    """The laundering path stays SHUT: both service headers + a public caller is refused.

    This is the case the original guard existed for, and narrowing its scope must not reopen it.
    """
    with pytest.raises(PermissionDeniedError):
        security.authenticate(
            _request(oidc=_verifier()),
            _settings(service_subjects="medallion"),
            None,
            dapr_api_token="the-estate-service-token",
            x_lance_service_identity="medallion",
            dapr_caller_app_id=_GATEWAY,
        )


def test_public_caller_cannot_launder_even_while_holding_a_valid_bearer() -> None:
    """A real user's token must not become a ladder into the SERVICE door.

    Presenting both service headers is a request to be authenticated as a SERVICE; a public caller
    is refused for that regardless of what else they carry.
    """
    with pytest.raises(PermissionDeniedError):
        security.authenticate(
            _request(oidc=_verifier()),
            _settings(service_subjects="medallion"),
            _creds(),
            dapr_api_token="the-estate-service-token",
            x_lance_service_identity="medallion",
            dapr_caller_app_id=_GATEWAY,
        )


def test_anonymous_through_the_gateway_is_unauthenticated_not_permitted() -> None:
    """No bearer, no service headers: 401, never a silent pass."""
    with pytest.raises(UnauthenticatedError):
        security.authenticate(
            _request(oidc=_verifier()),
            _settings(),
            None,
            dapr_caller_app_id=_GATEWAY,
        )


# --------------------------------------------------------------------------- #
# the non-proxied paths keep working
# --------------------------------------------------------------------------- #


def test_a_non_public_caller_is_never_refused_as_a_public_front_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine service invocation must not hit the public-caller refusal at all.

    With no `APP_API_TOKEN` configured the door is CLOSED, and since the two service doors were unified the
    two call sites that is a 401 NAMING the missing token, not a fall-through to OIDC — which
    answered the same request with "Missing bearer token" and sent operators to the IdP. Either way
    the point of the test is the TYPE of refusal: `Unauthenticated` (we could not authenticate you),
    NOT `PermissionDenied` (you are barred as a public front door).

    The `delenv` makes that precondition REAL. It was inherited from the ambient environment, so a
    developer with `APP_API_TOKEN` exported got the same 401 from a different branch (a rejected
    credential) and the prose above described a path the run never took.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(UnauthenticatedError):
        security.authenticate(
            _request(oidc=_verifier()),
            _settings(service_subjects="medallion"),
            None,
            dapr_api_token="",
            x_lance_service_identity="medallion",
            dapr_caller_app_id="medallion",
        )


def test_direct_call_with_no_dapr_header_is_unaffected() -> None:
    """The shape every pre-existing test used — must keep behaving identically."""
    token = security.authenticate(_request(oidc=_verifier()), _settings(), _creds())
    assert token is not None and token.sub == _SUB


# --------------------------------------------------------------------------- #
# §2.8 — the privileged door OPENS through catalog.authenticate itself
# --------------------------------------------------------------------------- #


def test_catalog_authenticate_passes_the_resolver_so_the_privileged_door_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """the original defect was one missing kwarg at THIS call site — the shared
    service_principal had the dedicated_token parameter and the catalog never passed it, so its
    privileged branch hard-refused every privileged subject no matter what was seeded. The prior
    tests all built the resolver in-test and called service_principal directly, so deleting the
    kwarg reproduced the defect with everything green. This goes through catalog.authenticate:
    delete `dedicated_token=` from security.py and it fails."""
    from service_kit.governed import dapr_auth

    monkeypatch.setenv("APP_API_TOKEN", "shared-token")
    dapr_auth._secret_bundle.cache_clear()
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_dapr_secret",
        lambda *_a, **_k: {"service-token-service-trainer": "trainer-own"},
    )
    settings = _settings(
        service_subjects="service-trainer",
        privileged_subjects="service-trainer",
        dapr_secret_store="lance-secrets",
        dapr_secret_key="lance",
    )

    admitted = security.authenticate(
        _request(oidc=_verifier()),
        settings,
        None,
        dapr_api_token="trainer-own",
        x_lance_service_identity="service-trainer",
        dapr_caller_app_id="medallion-producer",
    )
    assert admitted is not None and admitted.sub == "service-trainer"
    assert admitted.iss == "rask://service-door"

    # The refusal is the shared door's 401 (fastapi.HTTPException — the shared service_principal's
    # own type, not the catalog's problem classes; the status is what matters to the caller).
    with pytest.raises(Exception, match="may not claim"):
        security.authenticate(
            _request(oidc=_verifier()),
            settings,
            None,
            dapr_api_token="shared-token",
            x_lance_service_identity="service-trainer",
            dapr_caller_app_id="medallion-producer",
        )
    dapr_auth._secret_bundle.cache_clear()
