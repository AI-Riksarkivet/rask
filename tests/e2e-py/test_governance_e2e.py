"""Governance end-to-end: catalog authz + lineage provenance, one flow.

Runs against the full governance stack (``make governance-chain``):
catalog (OIDC + OpenFGA) **+** lineage-api (OpenLineage → its feed and edge list, Lance tables in the
catalog's provenance namespace), with the catalog's ``LANCE_LINEAGE_EMIT_ENABLED`` on. Skipped unless ``LANCE_E2E_AUTH_SERVER`` and
``LANCE_E2E_LINEAGE_URL`` are set and reachable.

Asserts the dataops/governance story end to end:
  1. anon create → 401 (OIDC enforced);
  2. alice creates namespace + bronze table → 200 (app seeds owner);
  3. an outsider (no grant) describe → 403; alice (owner) describe → 200 (authz cascade);
  4. the catalog recorded alice as the **verified** author of bronze's create on lineage's ``/events`` feed;
  5. a promote run (bronze → silver, as a user's pipeline emits) makes silver derived from bronze;
  6. a forced rename carries a direct grant and the protection record to the new name, and leaves no
     tuple naming the old one ([[LH-215]]), read off the OpenFGA store itself.

(The lineage service runs with its own auth OFF in this overlay; the gate is unit-tested +
has its own e2e. This test focuses on catalog authz + provenance authorship + tier lineage.)
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest
import requests
from principals import DEX_CLIENT, DEX_SCOPE, principal_of
from topology import OUTSIDER, assert_parent_exists, create_top_level

from lineage.models import author_sub_from_payload
from service_kit.governed import fga
from service_kit.governed.auth.write_model import STORE_NAME, fga_headers
from service_kit.governed.fga import newest_store
from service_kit.governed.principal import is_principal
from service_kit.openlineage import RUN_EVENT_SCHEMA_URL, custom_facet, run_id_for


#: OpenLineage ``producer`` URI for the promote event this suite emits — the spec requires one, and a
#: fixture standing in for a user's pipeline job has to look like one.
_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/tests/e2e-py/test_governance_e2e.py"

SERVER = os.environ.get("LANCE_E2E_AUTH_SERVER", "")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex")
# The kind Dex registers the sign-in client with a secret (sent here); the docker-compose
# overlay uses a public client. Default to the kind secret so the suite runs on the canonical stack; set
# LANCE_E2E_DEX_SECRET="" for a public-client Dex.
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
#: The OpenFGA the catalog asks, read directly where a claim is about the tuples the store holds.
FGA = os.environ.get("LANCE_E2E_FGA", "").rstrip("/")
FGA_STORE = os.environ.get("LANCE_E2E_FGA_STORE_ID", "")
FGA_TOKEN_FILE = os.environ.get("LANCE_E2E_FGA_TOKEN_FILE") or None

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
        "client_id": DEX_CLIENT,
        "username": username,
        "password": "password",
        "scope": DEX_SCOPE,
    }
    if DEX_SECRET:  # confidential client (kind); omit for a public-client Dex (docker-compose)
        data["client_secret"] = DEX_SECRET
    resp = requests.post(f"{DEX}/token", data=data, timeout=10)
    body = resp.json()
    assert "id_token" in body, f"Dex token grant failed ({resp.status_code}): {body}"
    return body["id_token"]


def _sub(token: str) -> str:
    """The principal the catalog keys this bearer on ([[LH-063]])."""
    return principal_of(SERVER, token)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _get_json(url: str, headers: dict | None = None) -> dict:
    """GET + parse JSON, degrading to ``{}`` on any transport/non-JSON error (so polls fail cleanly).

    Lineage's reads (``/events``, ``/tables/{name}/derived``) are GOVERNED per table, so a bearer MUST be
    forwarded — an anonymous read 401s and would poll forever on ``None``.
    """
    try:
        return requests.get(url, headers=headers, timeout=10).json()
    except Exception:
        return {}


def _poll(fn: Callable[[], object], want: object, *, tries: int = 60, delay: float = 1.0) -> object:
    """Poll ``fn`` until it equals ``want`` — emission is async/fire-and-forget — or give up. Generous
    window: on kind the create's announcement crosses Dapr → NATS JetStream → lineage → the catalog's
    provenance namespace, slower than a direct HTTP emit."""
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
    outsider = _token(OUTSIDER)
    ah = {"Authorization": f"Bearer {alice}"}
    bh = {"Authorization": f"Bearer {outsider}"}
    ns = f"gov{os.getpid()}"
    # 2-level ids (namespace$table) — the real medallion pattern; the table's parent is the created
    # namespace `ns`, so create-on-parent is authorized (a 3-level id would need the intermediate namespace
    # created first, which an enforcing stack correctly requires).
    bronze, silver = f"{ns}$bronze", f"{ns}$silver"

    # 1. anon -> 401 (OIDC enforced)
    assert requests.post(f"{server}/v1/namespace/{ns}/create", json={}, timeout=10).status_code == 401

    # 2. alice creates namespace + bronze table -> 200 (app seeds owner; catalog emits create-lineage).
    # Through whichever door this estate admits: with warehouses on, the root door answers 400
    # `must belong to a warehouse` — a TOPOLOGY refusal — and this suite read that as a governance
    # failure.
    assert_parent_exists(create_top_level(server, ns, ah), ns)
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    create = requests.post(f"{server}/v1/table/{bronze}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
    assert create.status_code == 200, create.text
    # alice also creates the silver table she'll promote INTO — so she owns it (can_write_data +
    # can_get_metadata). The lineage ingest below is governed (enforce_output_authz needs can_write_data on
    # every output), so a promote whose output is a table alice doesn't own would 403.
    sc = requests.post(f"{server}/v1/table/{silver}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
    assert sc.status_code == 200, sc.text

    # 3. the outsider (no grant) cannot describe -> 403 ; alice (owner) can -> 200 (cascade)
    assert requests.post(f"{server}/v1/table/{bronze}/describe", headers=bh, timeout=10).status_code == 403
    assert requests.post(f"{server}/v1/table/{bronze}/describe", headers=ah, timeout=10).status_code == 200

    # 4. lineage recorded alice as the VERIFIED author of bronze's create (emission is async -> poll).
    # The feed is GOVERNED per table, so forward alice's bearer — an anon read 401s -> None.
    def _bronze_authors() -> set[str | None]:
        rows = _get_json(f"{lineage}/events?table={bronze}&limit=500", ah).get("events", [])
        return {author_sub_from_payload(row.get("event")) for row in rows}

    authors = _poll(lambda: alice_sub in _bronze_authors(), True)
    assert authors is True, f"expected a feed row for {bronze} authored by {alice_sub}, got authors {_bronze_authors()}"

    # 5. a promote run (bronze -> silver), as a user's pipeline would emit.
    # Spec-true on purpose: this fixture is our stand-in for a real producer, so it has to carry what the
    # spec REQUIRES — a UUID ``runId`` (``format: uuid``; Marquez rejects anything else), ``producer`` and
    # ``schemaURL`` on the envelope, and ``_producer``/``_schemaURL`` on the custom ``author`` facet. The
    # first cut had none of those (``runId`` was ``promote-<pid>``) and the ingest accepted it, so what the
    # test really pinned was our own tolerance, not the contract (found 2026-07-26 — the event was still
    # sitting in the live /events feed as the one non-conforming non-reconcile event).
    promote: dict[str, Any] = {
        "eventType": "COMPLETE",
        "eventTime": "2026-06-24T00:00:00+00:00",
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {
            "runId": run_id_for(f"promote-{ns}-{os.getpid()}"),
            "facets": {"author": custom_facet(_PRODUCER, name=alice_sub, sub=alice_sub)},
        },
        "job": {"namespace": "e2e-pipeline", "name": "promote_bronze_to_silver"},
        "inputs": [{"namespace": ns, "name": bronze}],
        "outputs": [{"namespace": ns, "name": silver}],
    }
    assert requests.post(f"{lineage}/api/v1/lineage", json=promote, headers=ah, timeout=10).status_code == 201

    # 6. silver is derived from bronze (the tier lineage, off the edge list). Governed read → forward alice's
    # bearer; alice reads both via her ownership of the parent namespace ``ns`` (create-on-parent cascade).
    def _bronze_derived() -> list[str]:
        derived = _get_json(f"{lineage}/tables/{bronze}/derived", ah).get("derived", [])
        return [d["table"] for d in derived]

    down = _poll(_bronze_derived, [silver])
    assert isinstance(down, list) and silver in down, f"expected {silver} derived from {bronze}, got {down}"

    # 7. Every subject the store holds names its identity provider ([[LH-063]]): each `user` a tuple names decodes to
    # a principal, `<idp-id>~<subject>`, the owner tuples this flow's creates seeded among them.
    users = _store_users()
    assert fga.user_object(alice_sub) in users, f"the store holds no tuple naming alice's principal {alice_sub}, so the claim would pass vacuously"
    bare = sorted(user for user in users if user != fga.USER_WILDCARD and not is_principal(fga.subject_of(user.removeprefix("user:"))))
    assert not bare, f"the store holds subjects that name no identity provider: {bare}"


def _store() -> tuple[dict[str, str], str]:
    """The headers and the id of the catalog's OpenFGA store, read directly."""
    assert FGA, "set LANCE_E2E_FGA: this claim is about the tuples the store holds"
    headers = fga_headers(FGA_TOKEN_FILE)
    named = newest_store(requests.get(f"{FGA}/stores", headers=headers, timeout=10).json().get("stores") or [], STORE_NAME)
    store = FGA_STORE or (str(named["id"]) if named else "")
    assert store, f"the OpenFGA at {FGA} holds no store named {STORE_NAME!r}"
    return headers, store


def _tuples_on(obj: str) -> list[dict[str, Any]]:
    """Every tuple the catalog's OpenFGA store holds whose object is ``obj``."""
    headers, store = _store()
    read = requests.post(f"{FGA}/stores/{store}/read", headers=headers, json={"tuple_key": {"object": obj}}, timeout=10)
    assert read.status_code == 200, read.text
    return read.json().get("tuples") or []


def _store_users() -> set[str]:
    """Every `user:` subject a tuple in the store names, across every page of an unfiltered read."""
    headers, store = _store()
    users: set[str] = set()
    continuation = ""
    while True:
        body: dict[str, Any] = {"page_size": 100, **({"continuation_token": continuation} if continuation else {})}
        read = requests.post(f"{FGA}/stores/{store}/read", headers=headers, json=body, timeout=10)
        assert read.status_code == 200, read.text
        page = read.json()
        users |= {t["key"]["user"] for t in page.get("tuples") or [] if t["key"]["user"].startswith("user:")}
        continuation = page.get("continuation_token") or ""
        if not continuation:
            return users


def test_a_forced_rename_keeps_a_direct_grant_and_protection_and_writes_no_tuple(stack: tuple[str, str]) -> None:
    """A table's grants are held by its uuid (ADR 0173 §8, [[LH-524]]): a rename keeps the uuid and writes no tuple, and a
    table created again at the old name is another uuid that holds nothing the renamed one did."""
    server, _ = stack
    alice, outsider = _token("alice@example.com"), _token(OUTSIDER)
    ah, bh = {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {outsider}"}
    ns = f"mv{os.getpid()}"
    source, dest = f"{ns}$src", f"{ns}$dst"
    assert_parent_exists(create_top_level(server, ns, ah), ns)
    try:
        rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
        assert requests.post(f"{server}/v1/table/{source}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30).status_code == 200
        granted = requests.post(
            f"{server}/management/v1/table/{source}/access/grant", headers=ah, json={"user": _sub(outsider), "relation": "reader"}, timeout=10
        )
        assert granted.status_code == 200, granted.text
        assert requests.post(f"{server}/v1/table/{source}/describe", headers=bh, timeout=10).status_code == 200, "precondition: the direct grant reads"
        assert requests.post(f"{server}/management/v1/table/{source}/protection", headers=ah, json={"protected": True}, timeout=10).status_code == 200
        described = requests.post(f"{server}/v1/table/{source}/describe", headers=ah, json={}, timeout=10).json()
        held = _tuples_on(f"table:{described['properties']['rask.uuid']}")

        renamed = requests.post(f"{server}/v1/table/{source}/rename", headers=ah, params={"force": "true"}, json={"new_table_name": "dst"}, timeout=30)

        assert renamed.status_code == 200, renamed.text
        assert _tuples_on(f"table:{described['properties']['rask.uuid']}") == held, "the rename wrote or removed a tuple"
        assert requests.post(f"{server}/v1/table/{dest}/describe", headers=bh, timeout=10).status_code == 200, "the direct reader lost the renamed table"
        assert requests.get(f"{server}/management/v1/table/{dest}/protection", headers=ah, timeout=10).json()["protected"] is True
        # A table created at the old name holds nothing the renamed one did.
        assert requests.post(f"{server}/v1/table/{source}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30).status_code == 200
        assert requests.get(f"{server}/management/v1/table/{source}/protection", headers=ah, timeout=10).json()["protected"] is False, (
            "a table created at the old id was born protected"
        )
        assert requests.post(f"{server}/v1/table/{source}/describe", headers=bh, timeout=10).status_code == 403, "a table created at the old name kept a grant"
    finally:
        # The leg runs against live estates too; what it made goes, protection and all, so its namespace can be unbound.
        for table in (source, dest):
            requests.post(f"{server}/v1/table/{table}/drop", headers=ah, params={"force": "true", "purge": "true"}, json={}, timeout=30)
