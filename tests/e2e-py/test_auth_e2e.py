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

#: The catalog joins identifier segments with this, NOT a dot. `catalog/core/config.py` defaults it to
#: `$` and `.docker/docker-compose.yml` sets `LANCE_NS_DELIMITER: "$$"` (compose-escaped `$`). Read from
#: the environment so the suite follows a stack configured with a different one, rather than asserting
#: against a delimiter only this file believes in.
DELIMITER = os.environ.get("LANCE_NS_DELIMITER", "$")

pytestmark = [pytest.mark.e2e, pytest.mark.auth]


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
    store = max(stores, key=lambda s: s["created_at"])["id"]
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

    # Valid token, no tuple → the create is ALLOWED, and that is the intended posture for THIS stack.
    #
    # This asserted 403 and was wrong about the stack it runs against (audit H2). Top-level creation
    # is self-serve unless `fga_lock_root_create` is set — `_create_parent_check` returns None for a
    # single-segment id, so there is no parent to check and nothing to deny. The estate decides that
    # per environment and has already decided it: `chart/values.yaml` ships `auth.lockRootCreate:
    # false`, `chart/values-prod.yaml` ships `true`. `scripts/e2e_stack.sh` sets `auth.enabled=true`
    # and does NOT set lockRootCreate, so this suite runs against the OPEN default — which is exactly
    # what `scripts/auth_e2e.sh` (the script CI runs) has always expected: `expect 200 ... "alice
    # create namespace"`. Two artifacts asserted opposite outcomes for one request; the shell script
    # was right and this was describing production.
    assert requests.post(f"{server}/v1/namespace/e2ens/create", headers=headers, json={}, timeout=10).status_code in (200, 409)

    # THE DENY THAT ACTUALLY EXISTS is on the parent, so prove it on a NESTED create.
    #
    # The old sequence granted `writer` on `namespace:e2ens` and then re-created `e2ens`, which
    # proved nothing: a create gates on the PARENT, never on the object being created, so that grant
    # was inert and the create had already succeeded for an unrelated reason. Creating a CHILD is
    # where the parent check bites — and it is the rung a locked-root estate falls back to anyway.
    # `e2ens{DELIMITER}e2child`, NOT `e2ens.e2child`. With the real delimiter a dot makes a single
    # ROOT-level namespace whose name merely contains a dot — so this create gated on the root, the
    # 403 below could pass for an unrelated reason (a locked root), and the `_grant` on
    # `namespace:e2ens` that follows was inert against it. That is precisely the defect the comment
    # above says was removed, reintroduced in a different disguise by the fix that removed it.
    child = f"e2ens{DELIMITER}e2child"
    assert requests.post(f"{server}/v1/namespace/{child}/create", headers=headers, json={}, timeout=10).status_code == 403

    # Grant writer ON THE PARENT → the nested create reaches the backend (200, or 409 if it exists).
    _grant(store, model, sub, "writer", obj)
    assert requests.post(f"{server}/v1/namespace/{child}/create", headers=headers, json={}, timeout=10).status_code in (200, 409)

    # Grant reader → describe succeeds.
    _grant(store, model, sub, "reader", obj)
    assert requests.post(f"{server}/v1/namespace/e2ens/describe", headers=headers, timeout=10).status_code == 200
