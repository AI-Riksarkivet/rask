"""A caller who can read the PARENT learns an absent child is absent, not that it is forbidden.

`authorize` is a router-wide dependency, so it answers before any endpoint runs: an object with no
tuples fails the FGA check and the request is refused 403 `PermissionDenied`, whether the object is
forbidden or simply does not exist. Measured on the deployed catalog 2026-09-11: an absent table under
a namespace alice holds `can_get_metadata`, `can_create_table` AND `can_delete` on answered 403 code 15
where the Lance Namespace spec prescribes 404 `TableNotFound` — a stock client cannot tell "ask someone
for access" from "you typed the wrong name".

THE OWNER'S RULE (2026-09-11): **404 on READ doors, 403 kept on destructive ones.** The estate's
no-existence-oracle property is deliberate and documented for `delete_warehouse` / `delete_project` /
`_set_warehouse_status`, which collapse PermissionDenied into NotFound precisely so a destructive door
cannot enumerate ids. That stays: a destructive door is a write door, refused by the tier judge, and the probe never
sees it. A read door is different: a caller holding the parent's read rung can already LIST the parent, so
"does this child exist" is not information the 403 was protecting.

THE SAFETY PROPERTY, which is the half worth more than the feature: an object that EXISTS and is
forbidden must still answer 403. Turning that into 404 would tell an unauthorized caller which names
are real — the oracle this estate refuses — so the probe converts only an absence, and only for a
caller who already holds the parent rung.

Every decision is the real model's, on a store of this test's own ([[LH-235]]): alice creates `db1` and two tables in
it through the doors, which seed the tuples, and grants bob `reader` on one of them, which lights `can_get_metadata` on
`db1` through its `child` edge and nothing on the other table. Carol holds nothing.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's error codes a stock client reads (`lance_docs/ns_catalog/spec.yaml`).
TABLE_NOT_FOUND, PERMISSION_DENIED = 4, 15


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """`db1` with `db1$shared`, which bob reads, and `db1$real`, which he does not; alice owns all three."""
    alice = governed_catalog.bearer("alice")
    client = governed_catalog.client
    assert client.post("/v1/namespace/db1/create", json={}, headers=alice).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    for table in ("shared", "real"):
        created = client.post(f"/v1/table/db1${table}/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
    granted = client.post("/management/v1/table/db1$shared/access/grant", json={"user": "dex~bob", "relation": "reader"}, headers=alice)
    assert granted.status_code == 200, granted.text
    return governed_catalog


@pytest.mark.parametrize(
    ("caller", "door", "status", "code"),
    [
        pytest.param("bob", "/v1/table/db1$ghost/describe", 404, TABLE_NOT_FOUND, id="an-absent-table-under-a-readable-parent-is-not-found"),
        pytest.param("bob", "/v1/table/db1$real/describe", 403, PERMISSION_DENIED, id="an-existing-forbidden-table-stays-forbidden"),
        pytest.param("bob", "/v1/table/db1$ghost/drop", 403, PERMISSION_DENIED, id="a-destructive-door-keeps-its-403-for-an-absent-table"),
        pytest.param("carol", "/v1/table/db1$ghost/describe", 403, PERMISSION_DENIED, id="no-parent-rung-no-answer"),
    ],
)
def test_a_denied_door_answers_not_found_only_for_an_absent_table_a_reader_of_its_parent_asks_after(
    estate: Any, caller: str, door: str, status: int, code: int
) -> None:
    answered = estate.client.post(door, json={}, headers=estate.bearer(caller))

    assert (answered.status_code, answered.json()["code"]) == (status, code), answered.text


NAMESPACE_NOT_FOUND = 1


@pytest.fixture
def cascaded(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``tree$kids`` with the subtree dropped by a CASCADE on ``tree``, as ``request.param`` names.

    ``recoverable`` and ``destroyed`` drop it with and without a grace period beside :func:`estate`'s ``db1``, where
    alice owns ``db1$sub$leaf`` too and drops ``db1$sub`` the same way; ``recreated`` drops it recoverably and bob
    creates a ``tree`` of his own.
    """
    layout = request.param
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "0" if layout == "destroyed" else "7")
    catalog = request.getfixturevalue("estate")
    alice = catalog.bearer("alice")
    for namespace in ("tree", "tree$kids", "db1$sub", "db1$sub$leaf"):
        assert catalog.client.post(f"/v1/namespace/{namespace}/create", json={}, headers=alice).status_code == 200
    for top in ("tree", "db1$sub"):
        dropped = catalog.client.post(f"/v1/namespace/{top}/drop", json={"behavior": "CASCADE"}, headers=alice)
        assert dropped.status_code == 200, dropped.text
    if layout == "recreated":
        assert catalog.client.post("/v1/namespace/tree/create", json={}, headers=catalog.bearer("bob")).status_code == 200
    return catalog


@pytest.mark.parametrize(
    ("cascaded", "caller", "namespace", "status", "code"),
    [
        pytest.param("recoverable", "alice", "tree", 404, NAMESPACE_NOT_FOUND, id="a-dropped-top-is-not-found-to-who-holds-it-in-the-trash"),
        pytest.param("recoverable", "alice", "tree$kids", 404, NAMESPACE_NOT_FOUND, id="its-child-is-not-found-to-who-holds-it-in-the-trash"),
        pytest.param("destroyed", "bob", "db1$sub$leaf", 404, NAMESPACE_NOT_FOUND, id="a-destroyed-child-is-not-found-to-a-reader-of-what-still-stands"),
        pytest.param("recoverable", "carol", "tree$kids", 403, PERMISSION_DENIED, id="a-caller-who-held-nothing-learns-nothing"),
        pytest.param("destroyed", "bob", "nosuch", 403, PERMISSION_DENIED, id="a-top-level-name-is-never-answered-from-its-warehouse"),
        pytest.param("recreated", "alice", "tree$kids", 403, PERMISSION_DENIED, id="a-trashed-grant-says-nothing-under-a-name-recreated-since"),
    ],
    indirect=["cascaded"],
)
def test_a_namespace_a_cascade_dropped_is_not_found_to_a_caller_who_held_a_right_above_it(
    cascaded: Any, caller: str, namespace: str, status: int, code: int
) -> None:
    answered = cascaded.client.post(f"/v1/namespace/{namespace}/describe", json={}, headers=cascaded.bearer(caller))

    assert (answered.status_code, answered.json()["code"]) == (status, code), answered.text
