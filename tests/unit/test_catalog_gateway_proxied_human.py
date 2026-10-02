"""Through the gateway a signed-in human is the human, and a service token is not accepted at all.

`/api/catalog/*` is the ONLY public path to the catalog, and every request the gateway proxies
carries `dapr-caller-app-id: gateway`. Measured on the live cluster 2026-08-06, isolated to that one
header (direct to svc/rask-catalog, same valid token both times): a door that refused on the header
before looking at the bearer answered 200 without it and 403 with it, so the proxied shape — the only
shape a real user produces — has to be the one these tests drive.

A projected service-account token arriving through the public front door is refused 403
([[LH-220]]): the gateway forwards a stranger's request through Dapr, and a service
principal is never something it may present on their behalf.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError

from catalog.api import security
from service_kit.governed.machine_identity import ServiceAccountVerifier, ServicePrincipal
from service_kit.governed.oidc import IDToken


_GATEWAY = "gateway"
_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
_INGEST = "system:serviceaccount:default:rask-sa-ingest"


def _request(*, oidc: object | None = None, sa_oidc: object | None = None) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(oidc=oidc, sa_oidc=sa_oidc))
    return cast(Request, SimpleNamespace(app=app))


def _settings(sa_issuer: str | None = None) -> Any:
    """Structural stand-in for `catalog.core.config.Settings`: the fields `authenticate` reads."""
    return SimpleNamespace(oidc_enabled=True, oidc_audience="lance-catalog", sa_issuer=sa_issuer)


def _human_verifier() -> object:
    return SimpleNamespace(verify=lambda _t: IDToken(iss="https://dex.example/dex", sub=_SUB, aud="lance-catalog", iat=0, exp=1 << 31))


def test_gateway_proxied_human_with_a_valid_bearer_authenticates() -> None:
    token = security.authenticate(
        _request(oidc=_human_verifier()),
        _settings(),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.real.jwt"),
        dapr_caller_app_id=_GATEWAY,
    )

    assert isinstance(token, IDToken)
    assert token.sub == _SUB


def test_a_service_token_through_the_public_front_door_is_refused(sa_issuer: Any) -> None:
    """A valid, mapped token for ingest, forwarded by the gateway, is refused."""
    verifier = ServiceAccountVerifier(
        sa_issuer.issuer,
        "rask-catalog",
        {_INGEST: "service-ingest"},
        cache_ttl=300,
        leeway=60,
        fetch_token_file=str(sa_issuer.fetch_token_file),
        ca_file=str(sa_issuer.ca_file),
    )

    with pytest.raises(PermissionDeniedError, match="public front door"):
        security.authenticate(
            _request(oidc=_human_verifier(), sa_oidc=verifier),
            _settings(sa_issuer.issuer),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=sa_issuer.mint("rask-sa-ingest", audience="rask-catalog")),
            dapr_caller_app_id=_GATEWAY,
        )


def test_only_a_persons_bearer_is_forwarded_to_the_object_store() -> None:
    """`web_identity` vending re-presents the raw bearer, so a service's catalog-audience token must answer None."""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.real.jwt")
    person = IDToken(iss="https://dex.example/dex", sub=_SUB, aud="lance-catalog", iat=0, exp=1 << 31)
    service = ServicePrincipal(subject="service-ingest", service_account=_INGEST)

    assert security.raw_bearer(credentials, person) == "a.real.jwt"
    assert security.raw_bearer(credentials, service) is None
