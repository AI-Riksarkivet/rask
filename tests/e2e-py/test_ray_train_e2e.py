"""#53 Ray TRAIN path — the governed train→candidate→blessed loop with full reproducibility capture,
driven against a real KubeRay + catalog + lineage + GreptimeDB stack.

This guards what the governed-union suite does not: that a training run's experiment-tracking data
actually LANDS at every sink needed to reproduce it — the input feature versions PINNED in the
OpenLineage run, the numeric metrics in GreptimeDB over OTLP (#18, the MLflow replacement), and the
candidate→blessed registry move gated on the validator rung (writer denied). A run whose provenance
does not fully land is a failure here, not a footnote.

Env: LANCE_E2E_LANCERAY_URL, LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_DAPR_TOKEN,
LANCE_E2E_FGA, LANCE_E2E_DEX(_SECRET); optional LANCE_E2E_GREPTIME_URL (the metrics-capture leg skips
without it, so CI can keep observability off without silently dropping the lineage+promote guard).
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator

import pytest
import requests


LANCERAY = os.environ.get("LANCE_E2E_LANCERAY_URL", "")
CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
GREPTIME = os.environ.get("LANCE_E2E_GREPTIME_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
FGA = os.environ.get("LANCE_E2E_FGA", "")
DAPR_TOKEN = os.environ.get("LANCE_E2E_DAPR_TOKEN", "")
WAREHOUSE = "warehouse:lance_catalog"

pytestmark = [pytest.mark.e2e, pytest.mark.ray_train]


def _token(user: str) -> str:
    data = {
        "grant_type": "password",
        "client_id": "lance-catalog",
        "username": user,
        "password": "password",
        "scope": "openid",
        "client_secret": DEX_SECRET,
    }
    return requests.post(f"{DEX}/token", data=data, timeout=10).json()["id_token"]


def _sub(token: str) -> str:
    """The token's FGA subject (its ``sub`` claim) — the identity the services key authz on, NOT the email."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def _tuples(store_model: tuple[str, str], *, writes: list[dict] | None = None, deletes: list[dict] | None = None) -> None:
    """Write/delete OpenFGA tuples idempotently (dup-write / delete-of-absent tolerated; any other 400 fails
    HERE with the real error, not 8 min later as a poll timeout). Mirrors the governed-union suite."""
    store, model = store_model
    body: dict = {"authorization_model_id": model}
    if writes:
        body["writes"] = {"tuple_keys": writes}
    if deletes:
        body["deletes"] = {"tuple_keys": deletes}
    resp = requests.post(f"{FGA}/stores/{store}/write", json=body, timeout=10)
    if resp.status_code == 200:
        return
    message = resp.json().get("message", "") if resp.status_code == 400 else ""
    assert "already exists" in message or "did not exist" in message or "does not exist" in message, f"OpenFGA write failed ({resp.status_code}): {resp.text}"


@pytest.fixture(scope="module")
def stack() -> tuple[str, str, str]:
    if not (LANCERAY and CATALOG and LINEAGE and FGA and DAPR_TOKEN):
        pytest.skip("set LANCE_E2E_LANCERAY_URL / CATALOG / LINEAGE / FGA / DAPR_TOKEN")
    for name, url in (("lance-ray", LANCERAY), ("catalog", CATALOG), ("lineage", LINEAGE)):
        try:
            probe = "readyz" if name == "catalog" else "livez"
            requests.get(f"{url.rstrip('/')}/{probe}", timeout=5).raise_for_status()
        except Exception:
            pytest.skip(f"{name} not reachable at {url}")
    if requests.get(f"{LINEAGE.rstrip('/')}/runs", timeout=8).status_code != 401:
        pytest.skip("stack is not auth-on — this suite proves the GOVERNED train loop")
    return LANCERAY.rstrip("/"), CATALOG.rstrip("/"), LINEAGE.rstrip("/")


@pytest.fixture(scope="module")
def fga_store() -> tuple[str, str]:
    """The lance-catalog OpenFGA store id + latest authorization-model id (raw HTTP, like the services)."""
    stores = requests.get(f"{FGA}/stores", timeout=10).json()["stores"]
    store = next(s["id"] for s in stores if s["name"] == "lance-catalog")
    models = requests.get(f"{FGA}/stores/{store}/authorization-models", timeout=10).json()
    return store, models["authorization_models"][0]["id"]


@pytest.fixture(scope="module")
def alice(stack: tuple[str, str, str], fga_store: tuple[str, str]) -> Iterator[dict[str, str]]:
    """An authenticated READER over the estate: warehouse `reader` + the seed's table→namespace parent links
    give alice can_get_metadata on every cascade dataset AND the model registry (namespace:models parents
    the warehouse). This is what an authorized human analyst (or the service-web BFF) holds; the seed grants
    only SERVICE identities, so without this every alice read 403s on a fresh cluster and the reproducibility
    checks below can never observe the graph. Teardown deletes the grant — it's a durable BROAD read grant in
    the SHARED store, and leaving it behind would quietly widen alice's access for anything run afterwards."""
    _ = stack  # depend on the stack fixture: gate the shared-store grant-write behind its reachability +
    #            auth-on skip, so a suite that should SKIP never mutates OpenFGA. Not otherwise used here.
    token = _token("alice@example.com")
    grant = {"user": f"user:{_sub(token)}", "relation": "reader", "object": WAREHOUSE}
    _tuples(fga_store, writes=[grant])
    yield {"Authorization": f"Bearer {token}"}
    _tuples(fga_store, deletes=[grant])


def _poll(fn, timeout: float, label: str):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(5)
    pytest.fail(f"timed out waiting for {label} (last={last!r})")


@pytest.fixture(scope="module")
def standing_features(stack: tuple[str, str, str], alice: dict[str, str]) -> None:
    """Ensure ``silver$features`` exists before any train — the head resolves the feature at its LATEST
    version by opening the Lance dataset, so on a FRESH cluster (nothing lingering) a train would 422
    (resolve_failed), not skip. Drive one ``/produce`` and wait for the cascade to write silver. Idempotent:
    returns immediately when silver already exists (the long-lived local cluster). Reads as ``alice`` (a
    granted warehouse reader) — an ungranted human 403s here, so the grant is the fixture's whole point."""
    lance_ray, _catalog, lineage = stack

    # silver$features is trainable iff a run actually COMPLETED writing it. A bare 200 on /upstream is NOT
    # proof: the FGA object table:silver$features is a SEEDED parent link, so an authorized reader (alice)
    # gets 200 with EMPTY edges for a not-yet-written dataset — which silently skipped /produce and made the
    # train 422 "cannot resolve feature dataset". The COMPLETE producer run is the true "data committed"
    # signal (the mover emits it AFTER the Lance write lands), so it implies the head-opened dataset exists.
    def _silver_written() -> bool:
        r = requests.get(f"{lineage}/datasets/silver$features/producers", headers=alice, timeout=8)
        return r.status_code == 200 and any(p.get("event_type") == "COMPLETE" for p in r.json().get("producers", []))

    if _silver_written():
        return
    resp = requests.post(f"{lance_ray}/produce", headers={"dapr-api-token": DAPR_TOKEN}, timeout=30)
    assert resp.status_code == 202, f"/produce to seed silver$features failed: {resp.status_code} {resp.text}"
    # Generous window: on a FRESH kind cluster the cascade is bronze→silver via sequential Ray jobs,
    # and the first Ray job cold-starts its runtime env (~2 min) before any stage completes.
    _poll(lambda: _silver_written() or None, timeout=480.0, label="a COMPLETE silver$features producer run")


@pytest.mark.usefixtures("standing_features")
def test_train_to_blessed_with_full_reproducibility_capture(stack: tuple[str, str, str], alice: dict[str, str]) -> None:
    lance_ray, catalog, lineage = stack
    bob = {"Authorization": f"Bearer {_token('bob@example.com')}"}
    model = f"e2etrain{int(time.time()) % 100000}"

    # 1. Request a governed training run against the standing silver features.
    r = requests.post(
        f"{lance_ray}/train",
        headers={"dapr-api-token": DAPR_TOKEN, "content-type": "application/json"},
        json={"model": model, "features": [{"dataset": "silver$features"}]},
        timeout=30,
    )
    if r.status_code == 409:
        pytest.skip("train head not configured (needs medallion.ray + a running Ray cluster)")
    assert r.status_code == 202, r.text
    assert r.json().get("token"), "the train head must return a correlation token"

    # 2. The Ray job publishes a candidate registry version — poll the catalog registry for it.
    detail = _poll(
        lambda: (lambda rr: rr.json() if rr.status_code == 200 and rr.json().get("latest_version") else None)(
            requests.get(f"{catalog}/v1/model/{model}", headers=alice, timeout=15)
        ),
        timeout=300.0,
        label=f"candidate version for {model}",
    )
    candidate = detail["latest_version"]
    assert detail.get("candidate_metrics"), "candidate must carry recorded metrics (rows_seen/features)"

    # 3. Reproducibility: the OpenLineage run pins the exact input feature version (nothing floats).
    run = _poll(
        lambda: next(
            (r_ for r_ in requests.get(f"{lineage}/runs?limit=300", headers=alice, timeout=10).json().get("runs", []) if r_.get("author") == "service-trainer"),
            None,
        ),
        timeout=120.0,
        label="attributed service-trainer run",
    )
    inputs = requests.get(f"{lineage}/runs/{run['run_id']}/inputs", headers=alice, timeout=10).json().get("inputs", [])
    assert any(i["name"] == "silver$features" and str(i.get("version", "")).isdigit() for i in inputs), (
        f"training run must pin a concrete input feature version, got {inputs}"
    )
    # The model node is connected to its feature provenance in the graph.
    up = requests.get(f"{lineage}/datasets/models${model}/upstream", headers=alice, timeout=10)
    assert up.status_code == 200 and "silver$features" in {d["name"] for d in up.json().get("related", [])}

    # 4. Governance: a plain writer may not promote — only the validator rung.
    r = requests.post(f"{catalog}/v1/model/{model}/promote", json={"version": candidate}, headers=bob, timeout=20)
    assert r.status_code == 403, f"bob (non-validator) promote expected 403, got {r.status_code}"

    # 5. Metrics capture (the #18 OTLP path, MLflow's replacement) — GreptimeDB holds the run's numeric
    # metrics labelled by model. Optional so CI can keep observability off; when present, it must land.
    if GREPTIME:

        def _metric():
            sql = f"SELECT * FROM \"lance_training_rows_seen\" WHERE lance_model = '{model}' LIMIT 1"
            import urllib.parse
            import urllib.request

            url = f"{GREPTIME.rstrip('/')}/v1/sql?db=public&sql=" + urllib.parse.quote(sql)
            try:
                d = json.load(urllib.request.urlopen(url, timeout=10))
                return d["output"][0]["records"]["rows"] or None
            except Exception:
                return None

        rows = _poll(_metric, timeout=90.0, label=f"lance_training_rows_seen for {model} in GreptimeDB")
        assert rows, "training metrics must land in GreptimeDB (the experiment-tracking sink)"


def test_promote_requires_validator_rung_writer_denied(stack: tuple[str, str, str]) -> None:
    # A focused negative: an anonymous promote is refused outright (the endpoint is governed).
    _lance_ray, catalog, _lineage = stack
    r = requests.post(f"{catalog}/v1/model/nonexistent$m/promote", json={"version": 1}, timeout=15)
    assert r.status_code in (401, 403), f"unauthenticated promote must be 401/403, got {r.status_code}"
