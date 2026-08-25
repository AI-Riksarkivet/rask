"""Live OIDC (Dex) + OpenFGA authorization end-to-end.

Runs against the auth stack started with the compose overlay
(stood up by ``make auth-chain``). Skipped
unless ``LANCE_E2E_AUTH_SERVER`` is set and the stack is reachable.

Asserts the full chain: no token → 401, valid Dex token without a tuple → 403,
allowed once a writer/reader tuple is granted in OpenFGA.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

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


#: Dex's `lance-catalog` client is CONFIDENTIAL, so the password grant needs its secret. Without it
#: Dex answers `{"error":"invalid_client","error_description":"Invalid client credentials."}` and this
#: suite died on `KeyError: 'id_token'` — an error naming the response shape rather than the missing
#: credential. Every other mint in the repo passes it (scripts/auth_chain.sh, verify_merge_lineage.sh,
#: seed_dev_estate.sh); this one did not. The default is the dev fixture the chart ships
#: (`chart/values.yaml` frontend.oidc.clientSecret) — a real deployment overrides it.
CLIENT_ID = os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "lance-catalog")
CLIENT_SECRET = os.environ.get("LANCE_E2E_OIDC_CLIENT_SECRET", "lance-catalog-secret")


#: The warehouse to create this suite's top-level namespace under, when the estate has warehouses.
#: Empty targets the shared root door, correct only where `catalog.warehouses.enabled` is off.
WAREHOUSE = os.environ.get("LANCE_E2E_WAREHOUSE", "")


def _create_top_level(server: str, name: str, headers: dict[str, str]) -> requests.Response:
    """Create a top-level namespace through whichever door this estate actually admits.

    Which door is right is a property of the ESTATE, not of this suite. With warehouses enabled the
    root door answers 400 `top-level namespace '<n>' must belong to a warehouse` — a TOPOLOGY refusal,
    not an authz one — so the namespace has to come in through its warehouse. Measured 2026-08-25:
    this suite pinned the root door, and everything nested under the parent it then failed to create
    404'd, which reads as a missing grant rather than a missing parent.
    """
    if WAREHOUSE:
        return requests.post(
            f"{server}/v1/warehouses/{WAREHOUSE}/namespaces",
            headers=headers,
            json={"namespace": name, "adopt_existing": True},
            timeout=10,
        )
    return requests.post(f"{server}/v1/namespace/{name}/create", headers=headers, json={}, timeout=10)


def _token() -> str:
    resp = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": "alice@example.com",
            "password": "password",
            "scope": "openid",
        },
        timeout=10,
    )
    body = resp.json()
    # Say WHY, not just that a key is absent — a bad secret and an unreachable Dex look identical
    # through a KeyError, and they need opposite fixes.
    assert "id_token" in body, f"Dex issued no id_token (HTTP {resp.status_code}): {body}"
    return body["id_token"]


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
    # what `scripts/auth_chain.sh` (the assertions CI runs) has always expected: `expect 200 ... "alice
    # create namespace"`. Two artifacts asserted opposite outcomes for one request; the shell script
    # was right and this was describing production.
    #
    # ASSERTED AS "not an authz denial", not as 200/409. On an estate with `catalog.warehouses.enabled`
    # the same request comes back 400 `top-level namespace 'e2ens' must belong to a warehouse` — the
    # request got PAST authz and was refused on TOPOLOGY, which is this assertion passing, not failing.
    # Pinning 200/409 encoded a warehouse-less estate into a test whose subject is the auth chain, and
    # it broke the moment the estate grew warehouses (measured 2026-08-25).
    allowed = _create_top_level(server, "e2ens", headers)
    assert allowed.status_code not in (401, 403), (
        f"a valid token with no tuple must not be DENIED here (lockRootCreate is off on this stack); got {allowed.status_code}: {allowed.text[:200]}"
    )
    # The REST of this suite nests under `e2ens`, so it has to exist however this estate makes one.
    assert allowed.status_code in (200, 409), (
        f"could not create the parent this suite nests under: {allowed.status_code} {allowed.text[:220]} — "
        f"on a warehouses-enabled estate a top-level namespace must belong to one, so set "
        f"LANCE_E2E_WAREHOUSE to a warehouse id you can write to"
    )

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
    # …AND ON A PARENT THIS CALLER PROVABLY HOLDS NO RUNG ON — which `e2ens` is not, structurally
    # rather than on this one estate. Creating a namespace seeds the CREATOR as its `owner`
    # (`seed_ownership`), and the model reduces the create door to the rung ownership already carries
    # (`define can_create_namespace: writer`, `define writer: … or owner`). So the caller that made
    # `e2ens` can always create under it, and the 403 this asserts could never fire. Measured live
    # 2026-08-25: the nested create under `e2ens` came back 200.
    #
    # Revoking that owner tuple would not rescue it either: `writer` also inherits `from parent`, and
    # the namespace hangs off the very warehouse the caller had to hold the create rung on to make it.
    # A parent with NO tuples and NO ancestry is the only one where the parent check is the only thing
    # that can answer — so the deny is unambiguous rather than merely observed.
    orphan = f"e2orphan{uuid.uuid4().hex[:8]}"
    child = f"{orphan}{DELIMITER}e2child"
    denied = requests.post(f"{server}/v1/namespace/{child}/create", headers=headers, json={}, timeout=10)
    assert denied.status_code == 403, f"a nested create under an unowned parent must be denied; got {denied.status_code}: {denied.text[:200]}"

    # Grant writer ON THAT PARENT → the nested create is no longer denied. It may still be refused for
    # a NON-authz reason (the parent does not exist), which is the point: the 403 is gone.
    _grant(store, model, sub, "writer", f"namespace:{orphan}")
    allowed = requests.post(f"{server}/v1/namespace/{child}/create", headers=headers, json={}, timeout=10)
    assert allowed.status_code not in (401, 403), f"the writer grant on the parent must lift the deny; got {allowed.status_code}: {allowed.text[:200]}"

    # Grant reader → describe succeeds.
    _grant(store, model, sub, "reader", obj)
    assert requests.post(f"{server}/v1/namespace/e2ens/describe", headers=headers, timeout=10).status_code == 200
