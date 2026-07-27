"""Governance end-to-end: catalog authz + lineage provenance, one flow.

Runs against the full governance stack (``scripts/governance_e2e.sh``):
catalog (OIDC + OpenFGA) **+** lineage-api (OpenLineage → Apache AGE), with the catalog's
``LANCE_LINEAGE_EMIT_ENABLED`` on. Skipped unless ``LANCE_E2E_AUTH_SERVER`` and
``LANCE_E2E_LINEAGE_URL`` are set and reachable.

Asserts the dataops/governance story end to end:
  1. anon create → 401 (OIDC enforced);
  2. alice creates namespace + bronze table → 200 (app seeds owner);
  3. bob (no grant) describe → 403; alice (owner) describe → 200 (authz cascade);
  4. the catalog recorded alice as the **verified** creator of bronze in the lineage graph;
  5. a promote run (bronze → silver, as a lance-ray job emits) makes silver's upstream = bronze.

(The lineage service runs with its own auth OFF in this overlay; the gate is unit-tested +
has its own e2e. This test focuses on catalog authz + provenance authorship + medallion lineage.)
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable

import pyarrow as pa
import pytest
import requests

from service_kit.openlineage import RUN_EVENT_SCHEMA_URL, custom_facet, run_id_for


#: OpenLineage ``producer`` URI for the promote event this suite emits — the spec requires one, and a
#: fixture standing in for a real lance-ray job has to look like one.
_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/tests/e2e/test_governance_e2e.py"

SERVER = os.environ.get("LANCE_E2E_AUTH_SERVER", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
# The kind Dex registers lance-catalog as a CONFIDENTIAL client (needs the secret); the docker-compose
# overlay uses a public client. Default to the kind secret so the suite runs on the canonical stack; set
# LANCE_E2E_DEX_SECRET="" for a public-client Dex.
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def stack() -> tuple[str, str]:
    if not (SERVER and LINEAGE):
        pytest.skip("set LANCE_E2E_AUTH_SERVER and LANCE_E2E_LINEAGE_URL to run the governance e2e")
    try:
        requests.get(f"{SERVER}/livez", timeout=5).raise_for_status()
        requests.get(f"{LINEAGE}/livez", timeout=5).raise_for_status()
        requests.get(f"{DEX}/.well-known/openid-configuration", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("governance stack (catalog / lineage / dex) not reachable")
    return SERVER.rstrip("/"), LINEAGE.rstrip("/")


def _token(username: str) -> str:
    data = {
        "grant_type": "password",
        "client_id": "lance-catalog",
        "username": username,
        "password": "password",
        "scope": "openid",
    }
    if DEX_SECRET:  # confidential client (kind); omit for a public-client Dex (docker-compose)
        data["client_secret"] = DEX_SECRET
    resp = requests.post(f"{DEX}/token", data=data, timeout=10)
    body = resp.json()
    assert "id_token" in body, f"Dex token grant failed ({resp.status_code}): {body}"
    return body["id_token"]


def _sub(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _get_json(url: str, headers: dict | None = None) -> dict:
    """GET + parse JSON, degrading to ``{}`` on any transport/non-JSON error (so polls fail cleanly).

    The lineage dataset reads (/creator, /upstream, …) are GOVERNED (``require_metadata_access`` on the
    router), so a bearer MUST be forwarded — an anonymous read 401s and would poll forever on ``None``.
    """
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception:
        return {}


def _poll(fn: Callable[[], object], want: object, *, tries: int = 60, delay: float = 1.0) -> object:
    """Poll ``fn`` until it equals ``want`` — emission is async/fire-and-forget — or give up. Generous
    window (40s): on kind the create-lineage crosses Dapr → NATS JetStream → lineage → AGE, slower than the
    docker-compose direct-HTTP path this test was originally written against."""
    last: object = None
    for _ in range(tries):
        last = fn()
        if last == want:
            return last
        time.sleep(delay)
    return last


def test_governance_flow(stack: tuple[str, str]) -> None:
    server, lineage = stack
    alice = _token("alice@example.com")
    alice_sub = _sub(alice)
    bob = _token("bob@example.com")
    ah = {"Authorization": f"Bearer {alice}"}
    bh = {"Authorization": f"Bearer {bob}"}
    ns = f"gov{os.getpid()}"
    # 2-level ids (namespace$table) — the real medallion pattern; the table's parent is the created
    # namespace `ns`, so create-on-parent is authorized (a 3-level id would need the intermediate namespace
    # created first, which an enforcing stack correctly requires).
    bronze, silver = f"{ns}$bronze", f"{ns}$silver"

    # 1. anon -> 401 (OIDC enforced)
    assert requests.post(f"{server}/v1/namespace/{ns}/create", json={}, timeout=10).status_code == 401

    # 2. alice creates namespace + bronze table -> 200 (app seeds owner; catalog emits create-lineage)
    assert requests.post(f"{server}/v1/namespace/{ns}/create", headers=ah, json={}, timeout=10).status_code == 200
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    create = requests.post(f"{server}/v1/table/{bronze}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
    assert create.status_code == 200, create.text
    # alice also creates the silver table she'll promote INTO — so she owns it (can_write_data +
    # can_get_metadata). The lineage ingest below is governed (enforce_output_authz needs can_write_data on
    # every output), so a promote whose output is a table alice doesn't own would 403.
    sc = requests.post(f"{server}/v1/table/{silver}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
    assert sc.status_code == 200, sc.text

    # 3. bob (no grant) cannot describe -> 403 ; alice (owner) can -> 200 (cascade)
    assert requests.post(f"{server}/v1/table/{bronze}/describe", headers=bh, timeout=10).status_code == 403
    assert requests.post(f"{server}/v1/table/{bronze}/describe", headers=ah, timeout=10).status_code == 200

    # 4. lineage recorded alice as the VERIFIED creator (emission is async -> poll).
    # The read is GOVERNED (require_metadata_access), so forward alice's bearer — an anon read 401s -> None.
    creator = _poll(lambda: _get_json(f"{lineage}/datasets/{bronze}/creator", ah).get("creator"), alice_sub)
    assert creator == alice_sub, f"expected lineage creator={alice_sub}, got {creator}"

    # 5. a promote run (bronze -> silver), as a lance-ray job would emit.
    # Spec-true on purpose: this fixture is our stand-in for a real producer, so it has to carry what the
    # spec REQUIRES — a UUID ``runId`` (``format: uuid``; Marquez rejects anything else), ``producer`` and
    # ``schemaURL`` on the envelope, and ``_producer``/``_schemaURL`` on the custom ``author`` facet. The
    # first cut had none of those (``runId`` was ``promote-<pid>``) and the ingest accepted it, so what the
    # test really pinned was our own tolerance, not the contract (found 2026-07-26 — the event was still
    # sitting in the live /events feed as the one non-conforming non-reconcile event).
    promote = {
        "eventType": "COMPLETE",
        "eventTime": "2026-06-24T00:00:00+00:00",
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {
            "runId": run_id_for(f"promote-{ns}-{os.getpid()}"),
            "facets": {"author": custom_facet(_PRODUCER, name=alice_sub, sub=alice_sub)},
        },
        "job": {"namespace": "lance-ray", "name": "promote_bronze_to_silver"},
        "inputs": [{"namespace": ns, "name": bronze}],
        "outputs": [{"namespace": ns, "name": silver}],
    }
    assert requests.post(f"{lineage}/api/v1/lineage", json=promote, headers=ah, timeout=10).status_code == 201

    # 6. silver's upstream includes bronze (the medallion lineage). Governed read → forward alice's bearer;
    # alice can read silver via her ownership of the parent namespace ``ns`` (create-on-parent cascade).
    def _silver_upstream() -> list[str]:
        related = _get_json(f"{lineage}/datasets/{silver}/upstream", ah).get("related", [])
        return [r["name"] for r in related]

    up = _poll(_silver_upstream, [bronze])
    assert isinstance(up, list) and bronze in up, f"expected {bronze} in silver upstream, got {up}"


def test_malformed_bearer_is_rejected(stack: tuple[str, str]) -> None:
    """OIDC boundary: a garbage / non-JWT bearer is rejected 401 (not 500, not allowed)."""
    server, _ = stack
    for bad in ("Bearer not-a-jwt", "Bearer a.b.c", "Bearer "):
        r = requests.post(f"{server}/v1/namespace/x/describe", headers={"Authorization": bad}, timeout=10)
        assert r.status_code == 401, f"{bad!r} → {r.status_code} (expected 401)"


def test_non_owner_cannot_rename_or_overwrite_anothers_table(stack: tuple[str, str]) -> None:
    """Security (the fixes committed 9d4de0a / 185c333): a non-owner is denied the destructive
    rename + Overwrite of another user's table — 403, so the true owner can't be evicted/seized."""
    server, _ = stack
    alice, bob = _token("alice@example.com"), _token("bob@example.com")
    ah, bh = {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}
    ns = f"sec{os.getpid()}"
    tbl = f"{ns}$owned"
    requests.post(f"{server}/v1/namespace/{ns}/create", headers=ah, json={}, timeout=10)
    rows = _ipc(pa.table({"id": pa.array([1], pa.int64())}))
    assert (requests.post(f"{server}/v1/table/{tbl}/create", headers={**ah, **ARROW}, data=rows, timeout=30)).status_code == 200

    # bob has no grant on alice's table → both destructive paths are denied (owner-tier gates).
    rn = requests.post(f"{server}/v1/table/{tbl}/rename", headers=bh, json={"new_table_name": "stolen"}, timeout=10)
    ov = requests.post(f"{server}/v1/table/{tbl}/create?mode=overwrite", headers={**bh, **ARROW}, data=rows, timeout=30)
    assert rn.status_code == 403, f"bob rename → {rn.status_code} (expected 403)"
    assert ov.status_code == 403, f"bob overwrite → {ov.status_code} (expected 403)"
    # and alice still owns it (not evicted)
    assert (requests.post(f"{server}/v1/table/{tbl}/describe", headers=ah, timeout=10)).status_code == 200
