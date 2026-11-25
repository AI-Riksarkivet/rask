"""A grant is held by the table's or namespace's uuid, so a rename moves none and a re-created name inherits none.

The catalog mints each table's and namespace's uuid when it comes into being and keeps it across a rename (ADR 0173 §2),
and every FGA object it writes is ``table:<uuid>`` or ``namespace:<uuid>``, its parent and child edges joining uuids
(ADR 0173 §8, [[LH-524]]). The doors stay addressed by name and resolve the uuid on the server (ADR 0173 §11). So:

- a reader granted on ``t`` reads it under its new name ``u``, and the rename writes no tuple;
- a table destroyed and created again under that name is another object, and the reader holds nothing on it;
- no name reaches OpenFGA, so a name OpenFGA could not hold as an object id (a space) is created and described.

Every decision is the real model's, on a store of this test's own ([[LH-235]]); every tuple is the one the door writes.
"""

import logging
from typing import Any

import pytest

from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.naming import UUID_PROPERTY


#: The logger of the catalog's authorization decisions (`catalog.api.authz.decisions`, which logs each refusal), named so the
#: catalog is imported by its fixture.
_GATE_LOGGER = "catalog.api.authz.decisions"


_WAREHOUSE = "warehouse:lance_catalog"


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's namespace ``lance_catalog$db`` holding ``lance_catalog$db$t``, which bob is granted ``reader`` on through the grant door."""
    assert governed_catalog.create_in_the_estate("alice", "db").status_code == 200
    governed_catalog.create_table("alice", "lance_catalog$db$t")
    governed_catalog.ok("/management/v1/table/lance_catalog$db$t/access/grant", "alice", json={"user": "dex~bob", "relation": "reader"})
    return governed_catalog


def test_a_reader_keeps_a_renamed_table_and_holds_nothing_on_one_created_again_under_its_name(estate: Any, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN401
    """The refusal is recorded twice: who was refused on the audit trail, and what was refused on the catalog's own log,
    which names no subject. The drop's revokes are recorded as a deletion's, never as a create taking its grants back."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    caplog.set_level(logging.INFO, logger=_GATE_LOGGER)
    held = estate.store.tuples()

    estate.ok("/v1/table/lance_catalog$db$t/rename", "alice", json={"new_table_name": "u"})

    assert estate.store.tuples() == held, "the rename wrote or removed a tuple"
    assert estate.client.post("/v1/table/lance_catalog$db$u/describe", json={}, headers=estate.bearer("bob")).status_code == 200, "bob lost the renamed table"
    estate.ok("/v1/table/lance_catalog$db$u/drop", "alice", json={})
    estate.create_table("alice", "lance_catalog$db$u")
    refused = estate.client.post("/v1/table/lance_catalog$db$u/describe", json={}, headers=estate.bearer("bob"))
    assert refused.status_code == 403, f"bob reads a table created under the name he was granted: {refused.text}"
    denied = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__["audit.outcome"] == "deny"]
    said = [r.__dict__ for r in caplog.records if r.name == _GATE_LOGGER and r.getMessage() == "access_denied"]
    assert ({r["audit.subject"] for r in denied}, len(said) >= 1, [r for r in said if "dex~bob" in repr(r)]) == ({"dex~bob"}, True, [])
    revoked = {r.__dict__["audit.origin"] for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__["audit.action"] == "access_tuple_delete"}
    assert revoked == {"lifecycle_delete"}


def test_every_tuple_a_create_writes_names_the_objects_uuid(estate: Any) -> None:  # noqa: ANN401
    estate.ok("/v1/namespace/lance_catalog$db$inner/create", "alice", json={})
    estate.ok("/v1/table/lance_catalog$db$inner$declared/declare", "alice", json={})
    namespace, inner = estate.uuid("namespace", "lance_catalog$db", "alice"), estate.uuid("namespace", "lance_catalog$db$inner", "alice")
    table, declared = estate.uuid("table", "lance_catalog$db$t", "alice"), estate.uuid("table", "lance_catalog$db$inner$declared", "alice")
    alice = estate.person("alice")

    assert estate.store.tuples() == {
        (alice, "owner", f"namespace:{namespace}"),
        (_WAREHOUSE, "parent", f"namespace:{namespace}"),
        (f"namespace:{namespace}", "child", _WAREHOUSE),
        (alice, "owner", f"table:{table}"),
        (f"namespace:{namespace}", "parent", f"table:{table}"),
        (f"table:{table}", "child", f"namespace:{namespace}"),
        (estate.person("bob"), "reader", f"table:{table}"),
        (alice, "owner", f"namespace:{inner}"),
        (f"namespace:{namespace}", "parent", f"namespace:{inner}"),
        (f"namespace:{inner}", "child", f"namespace:{namespace}"),
        (alice, "owner", f"table:{declared}"),
        (f"namespace:{inner}", "parent", f"table:{declared}"),
        (f"table:{declared}", "child", f"namespace:{inner}"),
    }


def test_a_table_whose_name_holds_a_space_is_created_and_described(estate: Any) -> None:  # noqa: ANN401
    estate.create_table("alice", "lance_catalog$db$sp ace")

    described = estate.ok("/v1/table/lance_catalog$db$sp ace/describe", "alice", json={})
    assert (estate.person("alice"), "owner", f"table:{described['properties'][UUID_PROPERTY]}") in estate.store.tuples()


def test_a_check_openfga_does_not_answer_is_recorded_and_refuses_the_request(
    estate: Any,  # noqa: ANN401
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage is the deployment's, and the trail says so: one `failure` record naming the relation, never a silence
    and never a denial against the caller ([[LH-585]])."""
    from lance_namespace import ServiceUnavailableError

    from lakehouse.governed import fga_reads

    async def unanswered(_client: Any, *, user: str, relation: str, obj: str) -> bool:  # noqa: ANN401, ARG001 — the SDK's client
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_reads, "check", unanswered)
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)

    refused = estate.client.post("/v1/table/lance_catalog$db$t/describe", json={}, headers=estate.bearer("alice"))

    assert refused.status_code == 503, refused.text
    failures = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__["audit.outcome"] == "failure"]
    assert [(r["audit.action"], r["audit.relation"], r["audit.subject"], r["audit.reason"]) for r in failures] == [
        ("authz", "can_get_metadata", "dex~alice", "authz_unavailable")
    ]
