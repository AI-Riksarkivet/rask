"""OIDC authentication wiring tests (our logic only — no PyJWT/network).

The verifier is faked, so these assert our dependency's behavior: off → open,
on + no token → 401 problem+json, on + token → delegates to the verifier.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from catalog.core.config import Settings, get_settings
from fastapi.testclient import TestClient
from lance_namespace import ListTablesResponse, UnauthenticatedError

from service_kit.governed.oidc import IDToken


def _enabled_settings() -> Settings:
    # model_validate (not Settings(...)) so field-name keys validate cleanly: the
    # fields carry LANCE_* aliases and populate_by_name accepts the names at runtime.
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def test_oidc_disabled_leaves_routes_open(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=[])
    assert client.get("/v1/table").status_code == 200


def test_oidc_enabled_missing_token_is_401_problem_json(client: TestClient) -> None:
    client.app.dependency_overrides[get_settings] = _enabled_settings
    client.app.state.oidc = MagicMock()  # a verifier is present
    resp = client.get("/v1/table")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 16  # UNAUTHENTICATED


def test_oidc_enabled_valid_token_passes_through(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=[])
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="s", aud="lance", exp=1, iat=1)
    client.app.dependency_overrides[get_settings] = _enabled_settings
    client.app.state.oidc = verifier

    resp = client.get("/v1/table", headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 200
    verifier.verify.assert_called_once_with("tok")


@pytest.mark.parametrize("missing", ["issuer", "audience"])
def test_settings_rejects_enabled_oidc_without_provider(missing: str) -> None:
    kwargs: dict[str, object] = {"oidc_issuer": "https://idp.example", "oidc_audience": "lance"}
    kwargs[f"oidc_{missing}"] = None
    with pytest.raises(ValueError, match="OIDC"):
        Settings.model_validate({"oidc_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x", **kwargs})


def test_settings_rejects_fga_without_oidc() -> None:
    # FGA needs a user identity, so enabling it without OIDC must fail fast at construct.
    with pytest.raises(ValueError, match="OIDC"):
        Settings.model_validate({"fga_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"})


def test_oidc_verifier_failure_maps_to_401_problem_json(client: TestClient) -> None:
    """CONTRACT: a verifier that raises ``UnauthenticatedError`` (expired / bad-sig / wrong
    issuer / disallowed alg — all the cases unit-tested in tests/unit/test_oidc_verify.py)
    is rendered by our error handler as a 401 problem+json, never leaking the cause."""
    verifier = MagicMock()
    verifier.verify.side_effect = UnauthenticatedError("Invalid or expired token")
    client.app.dependency_overrides[get_settings] = _enabled_settings
    client.app.state.oidc = verifier

    resp = client.get("/v1/table", headers={"Authorization": "Bearer expired-or-forged"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == 16  # UNAUTHENTICATED
    verifier.verify.assert_called_once_with("expired-or-forged")


def test_oidc_enabled_empty_bearer_is_401(client: TestClient) -> None:
    """CONTRACT: an ``Authorization: Bearer`` header with no token value is unauthenticated."""
    client.app.dependency_overrides[get_settings] = _enabled_settings
    client.app.state.oidc = MagicMock()
    resp = client.get("/v1/table", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401
    assert resp.json()["code"] == 16


def test_oidc_token_subject_is_used_for_authz(client: TestClient, fake_ns: MagicMock) -> None:
    """CONTRACT: the parsed token's ``sub`` is the identity the rest of the stack sees.

    With FGA off this just confirms the verifier's ``IDToken`` flows through to the route
    (the authz layer's use of ``token.sub`` is covered in tests/integration/test_authz.py).
    """
    fake_ns.list_all_tables.return_value = ListTablesResponse(tables=[])
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    client.app.dependency_overrides[get_settings] = _enabled_settings
    client.app.state.oidc = verifier

    resp = client.get("/v1/table", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    verifier.verify.assert_called_once_with("good")
