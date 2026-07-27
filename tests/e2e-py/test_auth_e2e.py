"""Live OIDC (Dex) + OpenFGA authorization end-to-end.

Runs against the auth stack started with the compose overlay
(``.docker/docker-compose.auth.yml`` — see ``scripts/auth_e2e.sh``). Skipped
unless ``LANCE_E2E_AUTH_SERVER`` is set and the stack is reachable.

Asserts the full chain: no token → 401, valid Dex token without a tuple → 403,
allowed once a writer/reader tuple is granted in OpenFGA.
"""

from __future__ import annotations

import base64
import json
import os

import pytest
import requests


SERVER = os.environ.get("LANCE_E2E_AUTH_SERVER", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
FGA = os.environ.get("LANCE_E2E_FGA", "http://localhost:8080")

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def server() -> str:
    if not SERVER:
        pytest.skip("set LANCE_E2E_AUTH_SERVER (auth stack) to run the live auth e2e")
    try:
        requests.get(f"{SERVER}/livez", timeout=5).raise_for_status()
        requests.get(f"{DEX}/.well-known/openid-configuration", timeout=5).raise_for_status()
        requests.get(f"{FGA}/healthz", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("auth stack (server/dex/openfga) not reachable")
    return SERVER.rstrip("/")


def _token() -> str:
    resp = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "username": "alice@example.com",
            "password": "password",
            "scope": "openid",
        },
        timeout=10,
    )
    return resp.json()["id_token"]


def _sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def _store_and_model() -> tuple[str, str]:
    stores = requests.get(f"{FGA}/stores", timeout=10).json()["stores"]
    store = sorted(stores, key=lambda s: s["created_at"])[-1]["id"]
    models = requests.get(f"{FGA}/stores/{store}/authorization-models", timeout=10).json()
    return store, models["authorization_models"][0]["id"]


def _grant(store: str, model: str, sub: str, relation: str, obj: str) -> None:
    requests.post(
        f"{FGA}/stores/{store}/write",
        json={
            "writes": {"tuple_keys": [{"user": f"user:{sub}", "relation": relation, "object": obj}]},
            "authorization_model_id": model,
        },
        timeout=10,
    )


def test_oidc_and_openfga_authorization_chain(server: str) -> None:
    token = _token()
    sub = _sub(token)
    store, model = _store_and_model()
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    obj = "namespace:e2ens"

    # No token → 401 (OIDC enforced).
    assert requests.post(f"{server}/v1/namespace/e2ens/create", json={}, timeout=10).status_code == 401

    # Valid token, no tuple → 403 (OpenFGA denies).
    assert requests.post(f"{server}/v1/namespace/e2ens/create", headers=headers, json={}, timeout=10).status_code == 403

    # Grant writer → the create reaches the backend (200, or 409 if it already exists).
    _grant(store, model, sub, "writer", obj)
    assert requests.post(f"{server}/v1/namespace/e2ens/create", headers=headers, json={}, timeout=10).status_code in (200, 409)

    # Grant reader → describe succeeds.
    _grant(store, model, sub, "reader", obj)
    assert requests.post(f"{server}/v1/namespace/e2ens/describe", headers=headers, timeout=10).status_code == 200
