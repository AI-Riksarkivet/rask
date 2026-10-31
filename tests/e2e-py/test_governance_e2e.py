"""Governance end-to-end: catalog authz + lineage provenance, one flow.

Runs against the full governance stack (``make governance-chain``):
catalog (OIDC + OpenFGA) **+** lineage-api (OpenLineage → its feed and edge list, Lance tables in the
catalog's provenance namespace), with the catalog's ``LANCE_LINEAGE_EMIT_ENABLED`` on. Skipped unless ``LANCE_E2E_AUTH_SERVER`` and
``LANCE_E2E_LINEAGE_URL`` are set and reachable.

Asserts the dataops/governance story end to end:
  1. anon create → 401 (OIDC enforced);
  2. alice creates a namespace and its bronze table → 200, and each create seeds alice's ``owner``, and the table its
     ``parent`` edge, on the uuid objects the catalog minted, read off the OpenFGA store: no tuple is written by hand;
  3. alice writes the table she owns (owner cascades to writer);
  4. an outsider holding no grant is refused bronze's describe and its drop (403, PermissionDenied), each refusal leaving
     the table and its tuples as alice reads them and naming nothing the outsider did not send ([[LH-255]]);
  5. the catalog recorded alice as the **verified** author of bronze's create on lineage's ``/events`` feed;
  6. a promote run (bronze → silver, as a user's pipeline emits) makes silver derived from bronze;
  7. every subject the store holds names its identity provider;
  8. a forced rename keeps a direct grant and the protection record and writes no tuple, and a table created again at the
     old name is another uuid the grant does not reach ([[LH-524]]), answered to that grant's holder as an absent table.

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
from fga_store import read, tuples_on
from lance_namespace import ErrorCode
from principals import DEX_CLIENT, DEX_SCOPE, principal_of
from refusals import assert_refused, described, namespace_uuid
from topology import OUTSIDER, WAREHOUSE, assert_parent_exists, create_top_level

from lineage.models import author_sub_from_payload
from service_kit.governed import fga
from service_kit.governed.principal import is_principal
from service_kit.openlineage import RUN_EVENT_SCHEMA_URL, author_facet, run_id_for


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
    try:
        rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
        create = requests.post(f"{server}/v1/table/{bronze}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
        assert create.status_code == 200, create.text
        # alice also creates the silver table she'll promote INTO — so she owns it (can_write_data +
        # can_get_metadata). The lineage ingest below is governed (enforce_output_authz needs can_write_data on
        # every output), so a promote whose output is a table alice doesn't own would 403.
        sc = requests.post(f"{server}/v1/table/{silver}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
        assert sc.status_code == 200, sc.text
        # Each create seeded alice's `owner`, and the table its `parent` edge, on the uuid objects the catalog minted.
        ns_uuid = namespace_uuid(server, ns, ah)
        namespace_object = fga.namespace_object(ns_uuid)
        standing = described(server, bronze, ah)
        table_object = fga.table_object(standing["properties"]["rask.uuid"])
        alice_user = fga.user_object(alice_sub)
        assert (alice_user, "owner") in _held(namespace_object)
        assert {(alice_user, "owner"), (namespace_object, "parent")} <= _held(table_object)

        # 3. owning bronze, alice writes it (owner cascades to writer)
        appended = requests.post(f"{server}/v1/table/{bronze}/insert", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30)
        assert appended.status_code == 200, appended.text

        # 4. the outsider (no grant) is refused bronze's describe and its drop; alice still describes it, and the store holds
        # the tuples it held
        for door in ("describe", "drop"):
            assert_refused(
                lambda door=door: requests.post(f"{server}/v1/table/{bronze}/{door}", headers=bh, json={}, timeout=10),
                status=403,
                code=ErrorCode.PERMISSION_DENIED,
                unchanged=lambda: (described(server, bronze, ah), tuples_on(table_object)),
                foreign={alice_sub, standing["location"], standing["properties"]["rask.uuid"], ns_uuid, WAREHOUSE},
            )

        # 5. lineage recorded alice as the VERIFIED author of bronze's create (emission is async -> poll).
        # The feed is GOVERNED per table, so forward alice's bearer — an anon read 401s -> None.
        def _bronze_authors() -> set[str | None]:
            rows = _get_json(f"{lineage}/events?table={bronze}&limit=500", ah).get("events", [])
            return {author_sub_from_payload(row.get("event")) for row in rows}

        authors = _poll(lambda: alice_sub in _bronze_authors(), True)
        assert authors is True, f"expected a feed row for {bronze} authored by {alice_sub}, got authors {_bronze_authors()}"

        # 6. a promote run (bronze -> silver), as a user's pipeline would emit.
        # Spec-true on purpose: this fixture is our stand-in for a real producer, so it has to carry what the
        # spec REQUIRES — a UUID ``runId`` (``format: uuid``; Marquez rejects anything else), ``producer`` and
        # ``schemaURL`` on the envelope, and ``_producer``/``_schemaURL`` on the custom ``rask_author`` facet. The
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
                "facets": {"rask_author": author_facet(_PRODUCER, alice_sub)},
            },
            "job": {"namespace": "e2e-pipeline", "name": "promote_bronze_to_silver"},
            "inputs": [{"namespace": ns, "name": bronze}],
            "outputs": [{"namespace": ns, "name": silver}],
        }
        assert requests.post(f"{lineage}/api/v1/lineage", json=promote, headers=ah, timeout=10).status_code == 201

        # silver is derived from bronze (the tier lineage, off the edge list). Governed read → forward alice's
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
    finally:
        # The flow runs against live estates too: its tables go with their bytes, so the session's teardown can unbind
        # their namespace (`topology.unbind_created`).
        for table in (bronze, silver):
            requests.post(f"{server}/v1/table/{table}/drop", headers=ah, params={"purge": "true"}, json={}, timeout=30)


def _held(obj: str) -> set[tuple[str, str]]:
    """The ``(user, relation)`` pairs the store holds on ``obj``."""
    return {(user, relation) for user, relation, _ in tuples_on(obj)}


def _store_users() -> set[str]:
    """Every `user:` subject a tuple in the store names, across an unfiltered read."""
    return {held["key"]["user"] for held in read() if held["key"]["user"].startswith("user:")}


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
        first = described(server, source, ah)
        held = tuples_on(fga.table_object(first["properties"]["rask.uuid"]))

        renamed = requests.post(f"{server}/v1/table/{source}/rename", headers=ah, params={"force": "true"}, json={"new_table_name": "dst"}, timeout=30)

        assert renamed.status_code == 200, renamed.text
        assert tuples_on(fga.table_object(first["properties"]["rask.uuid"])) == held, "the rename wrote or removed a tuple"
        assert requests.post(f"{server}/v1/table/{dest}/describe", headers=bh, timeout=10).status_code == 200, "the direct reader lost the renamed table"
        assert requests.get(f"{server}/management/v1/table/{dest}/protection", headers=ah, timeout=10).json()["protected"] is True
        # A table created at the old name holds nothing the renamed one did: the renamed table's reader, who sees the
        # namespace through it, is answered as for an absent table ([[LH-236]]).
        assert requests.post(f"{server}/v1/table/{source}/create", headers={**ah, **ARROW}, data=_ipc(rows), timeout=30).status_code == 200
        assert requests.get(f"{server}/management/v1/table/{source}/protection", headers=ah, timeout=10).json()["protected"] is False, (
            "a table created at the old id was born protected"
        )
        again = described(server, source, ah)
        assert_refused(
            lambda: requests.post(f"{server}/v1/table/{source}/describe", headers=bh, json={}, timeout=10),
            status=404,
            code=ErrorCode.TABLE_NOT_FOUND,
            unchanged=lambda: tuples_on(fga.table_object(again["properties"]["rask.uuid"])),
            foreign={
                _sub(alice),
                again["location"],
                again["properties"]["rask.uuid"],
                first["properties"]["rask.uuid"],
                namespace_uuid(server, ns, ah),
                WAREHOUSE,
            },
        )
    finally:
        # The leg runs against live estates too; what it made goes, protection and all, so its namespace can be unbound.
        for table in (source, dest):
            requests.post(f"{server}/v1/table/{table}/drop", headers=ah, params={"force": "true", "purge": "true"}, json={}, timeout=30)
