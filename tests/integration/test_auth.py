"""OIDC authentication at the catalog's door: off → open, on + no token → 401 problem+json, on + a token the issuer
signed → admitted, on + one it no longer vouches for → 401 problem+json.

The person verifier is the one the catalog's lifespan builds, and the issuer is a real one on loopback (the root
conftest's `dex_issuer`), so a token is verified as the estate verifies it. Every way a verifier can fail a token is
`packages/lakehouse/tests/test_every_verifier_failure_is_a_401_or_a_503.py`'s.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import ListTablesResponse

from catalog.api.dependencies import request_settings
from catalog.core.config import Settings
from lakehouse.governed.auth_lifespan import person_verifier
from lakehouse.governed.idp_identity import IdpVerifier
from tests.app_state import app_of
from tests.catalog_settings import governed_settings


#: The audience the loopback issuer's tokens are minted for, and the connector its people sign in through.
_AUDIENCE = "lance-catalog"
_CONNECTOR = "local"


def _enabled_settings() -> Settings:
    """OIDC on and FGA off: what is under test is the authentication gate alone."""
    return governed_settings(fga_enabled=False)


def _issued_by(issuer: str) -> Settings:
    """:func:`_enabled_settings` trusting ``issuer``, a loopback one, for :data:`_AUDIENCE`."""
    return governed_settings(fga_enabled=False, oidc_issuer=issuer, oidc_audience=_AUDIENCE, oidc_allow_insecure=True)


def _verifier(settings: Settings) -> IdpVerifier:
    """The person verifier the catalog's lifespan builds from ``settings``; it fetches no key until it is asked to
    verify a token."""
    assert settings.oidc_audience is not None
    return person_verifier(settings, settings.oidc_person_providers, settings.oidc_audience)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("GET", "/v1/table", None, id="a-read"),
        # The caller is judged before the body is read (ADR 0203), under the settings the gate is given: malformed JSON
        # from a caller with no bearer is answered as unauthenticated, never as a body FastAPI parsed first.
        pytest.param("POST", "/v1/namespace/ns/create", b"{not json", id="a-body-it-never-reads"),
    ],
)
def test_oidc_enabled_missing_token_is_401_problem_json(client: TestClient, method: str, path: str, body: bytes | None) -> None:
    app_of(client).dependency_overrides[request_settings] = _enabled_settings
    app_of(client).state.oidc = _verifier(_enabled_settings())
    resp = client.request(method, path, content=body, headers={"Content-Type": "application/json"} if body else {})
    assert resp.status_code == 401, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 16  # UNAUTHENTICATED


def test_a_token_the_issuer_signed_is_admitted(client: TestClient, fake_ns: MagicMock, dex_issuer: Any) -> None:  # noqa: ANN401 — the root conftest's DexIssuer
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=[])
    settings = _issued_by(dex_issuer.issuer)
    app_of(client).dependency_overrides[request_settings] = lambda: settings
    app_of(client).state.oidc = _verifier(settings)

    resp = client.get("/v1/table", headers={"Authorization": f"Bearer {dex_issuer.mint('alice', connector=_CONNECTOR, audience=_AUDIENCE)}"})

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("missing", ["issuer", "audience"])
def test_settings_rejects_enabled_oidc_without_provider(missing: str) -> None:
    kwargs: dict[str, object] = {"oidc_issuer": "https://idp.example", "oidc_audience": "lance", "oidc_idp_id": "dex", "oidc_connectors": {"local": "dex"}}
    kwargs[f"oidc_{missing}"] = None
    with pytest.raises(ValueError, match="OIDC"):
        Settings.model_validate({"oidc_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x", **kwargs})


def test_settings_rejects_fga_without_oidc() -> None:
    # FGA needs a user identity, so enabling it without OIDC must fail fast at construct.
    with pytest.raises(ValueError, match="OIDC"):
        Settings.model_validate({"fga_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"})


def test_a_token_the_issuer_no_longer_vouches_for_is_401_problem_json(client: TestClient, dex_issuer: Any) -> None:  # noqa: ANN401 — the root conftest's DexIssuer
    """A token the verifier refuses, here one that expired an hour ago, is rendered by the catalog's error handler as a
    401 problem+json."""
    settings = _issued_by(dex_issuer.issuer)
    app_of(client).dependency_overrides[request_settings] = lambda: settings
    app_of(client).state.oidc = _verifier(settings)
    expired = dex_issuer.mint("alice", connector=_CONNECTOR, audience=_AUDIENCE, ttl=-3600)

    resp = client.get("/v1/table", headers={"Authorization": f"Bearer {expired}"})

    assert (resp.status_code, resp.headers["content-type"].split(";")[0], resp.json()["code"]) == (401, "application/problem+json", 16), resp.text


def test_oidc_enabled_empty_bearer_is_401(client: TestClient) -> None:
    """CONTRACT: an ``Authorization: Bearer`` header with no token value is unauthenticated."""
    app_of(client).dependency_overrides[request_settings] = _enabled_settings
    app_of(client).state.oidc = _verifier(_enabled_settings())
    resp = client.get("/v1/table", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    assert resp.json()["code"] == 16
