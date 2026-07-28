"""Per-subject user state against the DEPLOYED catalog, its real Dapr sidecar and the real state store.

The unit suite proves the logic against a stand-in sidecar. This proves the union nothing mocked can:
the catalog image that is actually running, a real Dex ID token, the ``lance-statestore`` component
resolving its DSN from OpenBao, and a Postgres row that outlives the pod that wrote it.

Four things only a live run can say:

* the component is **loaded for this app-id** — an unscoped one answers "component not found", which no
  unit test can produce;
* the key Dapr composes (``catalog||user-state:…``) is accepted by Postgres, so the ``||`` rule was read
  correctly rather than merely written down;
* **alice's document is not bob's**, through a real token pair rather than a fake verifier;
* the work **survives the pod**, which is the whole point of leaving ``localStorage``.

Run: ``kubectl port-forward svc/lance-ns-catalog 8000:8000`` + ``svc/lance-ns-dex 5556:5556``, then
``LANCE_E2E_CATALOG_URL=http://localhost:8000 uv run pytest tests/e2e-py/test_user_state_e2e.py -m user_state``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Any

import pytest
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
RELEASE = os.environ.get("LANCE_E2E_RELEASE", "lance-ns")

pytestmark = pytest.mark.user_state


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG:
        pytest.skip("set LANCE_E2E_CATALOG_URL (a port-forward to the deployed catalog) to run this")
    try:
        requests.get(f"{CATALOG}/livez", timeout=5).raise_for_status()
        requests.get(f"{DEX}/.well-known/openid-configuration", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("the deployed catalog / Dex is not reachable")
    return CATALOG.rstrip("/")


def _token(user: str) -> str:
    resp = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "client_secret": "lance-catalog-secret",
            "username": user,
            "password": "password",
            "scope": "openid email",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id_token"]


def _sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


@pytest.fixture(scope="module")
def alice() -> str:
    return _token("alice@example.com")


@pytest.fixture(scope="module")
def bob() -> str:
    return _token("bob@example.com")


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _graph(label: str) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "q1", "type": "query", "position": {"x": 0, "y": 0}},
            {"id": "s1", "type": "search", "position": {"x": 240, "y": 0}},
        ],
        "edges": [{"id": "q1-s1", "source": "q1", "target": "s1", "targetHandle": "in"}],
        "config": {"q1": {"q": label, "mode": "hybrid", "n": 12}},
        "tags": {"s1": ["review"]},
    }


def test_the_endpoint_is_deployed_not_merely_committed(catalog: str) -> None:
    """A 404 here means the running image predates the code — the failure that bit `#113`."""
    spec = requests.get(f"{catalog}/openapi.json", timeout=10).json()
    assert "/v1/user-state/workflow-graph" in spec["paths"]
    assert "/v1/user-state/saved-views" in spec["paths"]


def test_anonymous_is_401(catalog: str) -> None:
    assert requests.get(f"{catalog}/v1/user-state/workflow-graph", timeout=10).status_code == 401


def test_round_trip_against_the_real_state_store(catalog: str, alice: str) -> None:
    """The component is loaded for this app-id, and the key Dapr composes is one Postgres accepts.

    The round trip is faithful, not byte-identical, and the difference is deliberate: a PARTIAL node
    config comes back COMPLETE, because the server fills every field the client omitted. The first
    version of this test asserted byte-identity and failed here on a live run — usefully, because it is
    the assertion that had to be thought about rather than the code. Filling is correct: each default
    below is the same value the zone's own ``v.fallback`` would heal an absent key to
    (``workflow/persistence.ts``), so the client parses the completed document exactly as it would have
    parsed its own partial one. ``tests/unit/test_user_state.py`` pins the default table itself.
    """
    graph = _graph("alice's live canvas")
    put = requests.put(f"{catalog}/v1/user-state/workflow-graph", json=graph, headers=_auth(alice), timeout=15)
    assert put.status_code == 200, put.text
    assert put.json()["subject"] == _sub(alice)

    got = requests.get(f"{catalog}/v1/user-state/workflow-graph", headers=_auth(alice), timeout=15)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["exists"] is True
    # Everything the client sent survives verbatim…
    assert body["value"]["nodes"] == graph["nodes"]
    assert body["value"]["edges"] == graph["edges"]
    assert body["value"]["tags"] == graph["tags"]
    stored = body["value"]["config"]["q1"]
    assert {k: stored[k] for k in graph["config"]["q1"]} == graph["config"]["q1"]
    # …and what it omitted comes back as the zone's own fallbacks, never as nulls it cannot parse.
    assert stored["where"] == "" and stored["filters"] == {} and stored["enabled"] is True
    assert "minScore" not in stored and "capturedAtlasSelection" not in stored
    # The zone's valibot edge schema rejects an explicit null; absence is what it accepts.
    assert "label" not in body["value"]["edges"][0]


def test_bob_cannot_read_alices_state(catalog: str, alice: str, bob: str) -> None:
    """The governance claim, through two real Dex identities on one URL with no other difference."""
    requests.put(
        f"{catalog}/v1/user-state/workflow-graph",
        json=_graph("alice's live canvas"),
        headers=_auth(alice),
        timeout=15,
    ).raise_for_status()
    requests.put(
        f"{catalog}/v1/user-state/workflow-graph",
        json=_graph("bob's live canvas"),
        headers=_auth(bob),
        timeout=15,
    ).raise_for_status()

    hers = requests.get(f"{catalog}/v1/user-state/workflow-graph", headers=_auth(alice), timeout=15).json()
    his = requests.get(f"{catalog}/v1/user-state/workflow-graph", headers=_auth(bob), timeout=15).json()

    assert hers["subject"] == _sub(alice) and his["subject"] == _sub(bob)
    assert hers["subject"] != his["subject"]
    assert hers["value"]["config"]["q1"]["q"] == "alice's live canvas"
    assert his["value"]["config"]["q1"]["q"] == "bob's live canvas"


def test_saved_views_round_trip(catalog: str, alice: str) -> None:
    views = [
        {"name": "ambiguous speakers", "dataset": "", "spec": {"q": "speaker", "mode": "hybrid"}},
        {"name": "low confidence htr", "dataset": "corpus", "spec": {"q": "htr", "n": 50}},
    ]
    put = requests.put(f"{catalog}/v1/user-state/saved-views", json=views, headers=_auth(alice), timeout=15)
    assert put.status_code == 200, put.text
    got = requests.get(f"{catalog}/v1/user-state/saved-views", headers=_auth(alice), timeout=15).json()
    assert got["value"] == views  # order preserved, and no nulls added that TS would reject


def test_the_row_is_in_postgres_under_the_prefixed_key(catalog: str, alice: str) -> None:
    """Not a memo: the value is a row in the state store's table, under Dapr's own ``appid||key``.

    This is also the ``||`` rule proven from the other side — the separator Dapr adds is present exactly
    once, between the app-id and our key, because our key could not contribute one.
    """
    requests.put(
        f"{catalog}/v1/user-state/workflow-graph",
        json=_graph("alice's live canvas"),
        headers=_auth(alice),
        timeout=15,
    ).raise_for_status()
    from service_kit.governed.user_state import UserStateDocument, state_key

    key = state_key(_sub(alice), UserStateDocument.WORKFLOW_GRAPH)
    sql = f"select key from state where key = 'catalog||{key}'"  # noqa: S608 — key is base64url + literals
    out = subprocess.run(  # noqa: S603
        [
            "kubectl",
            "exec",
            f"{RELEASE}-age-0",
            "--",
            "psql",
            "-U",
            "lance",
            "-d",
            "daprstate",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        pytest.skip(f"could not reach the state-store database: {out.stderr.strip()[:200]}")
    assert out.stdout.strip() == f"catalog||{key}", out.stdout
    assert out.stdout.count("||") == 1


def test_the_work_survives_the_pod_that_wrote_it(catalog: str, alice: str) -> None:
    """The reason for leaving ``localStorage`` at all, taken to its logical end.

    Writes as alice, deletes the catalog pod, waits for the replacement, and reads again. A memo or an
    in-process cache would come back empty; a durable store comes back with her canvas.
    """
    marker = f"survives-{int(time.time())}"
    requests.put(
        f"{catalog}/v1/user-state/workflow-graph",
        json=_graph(marker),
        headers=_auth(alice),
        timeout=15,
    ).raise_for_status()

    kill = subprocess.run(
        ["kubectl", "delete", "pod", "-l", "app.kubernetes.io/component=catalog", "--wait=true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if kill.returncode != 0:
        pytest.skip(f"could not restart the catalog: {kill.stderr.strip()[:200]}")
    subprocess.run(  # noqa: S603
        ["kubectl", "rollout", "status", f"deploy/{RELEASE}-catalog", "--timeout=180s"],
        capture_output=True,
        text=True,
        check=False,
    )

    # `kubectl port-forward` binds to ONE pod, even when addressed to a Service, so deleting that pod
    # kills the tunnel for good — the first version of this test polled a socket that was never coming
    # back and reported "the catalog never came back", which would have read as a durability failure.
    # The test owns the forward it depends on.
    port = int(catalog.rsplit(":", 1)[1])
    subprocess.run(["pkill", "-f", f"port-forward svc/{RELEASE}-catalog"], check=False)  # noqa: S603, S607
    time.sleep(1)
    forward = subprocess.Popen(  # noqa: S603
        ["kubectl", "port-forward", f"svc/{RELEASE}-catalog", f"{port}:2333"],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert forward.poll() is None, "the replacement port-forward exited immediately"
    for _ in range(60):
        try:
            got = requests.get(f"{catalog}/v1/user-state/workflow-graph", headers=_auth(alice), timeout=5)
            if got.status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(2)
    else:
        pytest.fail("the catalog never came back through a fresh port-forward")

    assert got.json()["value"]["config"]["q1"]["q"] == marker
    # The forward is deliberately LEFT RUNNING: it replaces the one this test destroyed, so tearing it
    # down would leave the suite's own URL dead and every later run would SKIP — which reads as green.
    # A second run of this file skipped all seven tests for exactly that reason before this comment
    # existed. Restoring the precondition is the teardown.
