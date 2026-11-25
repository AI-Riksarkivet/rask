"""The two rungs between the data rungs ([[LH-570]]): a describer sees a table and reads none of it, and a modifier
runs its lifecycle and hands out nothing.

Seeing a table (its name in a listing, its describe answer with where it is stored) is `describer`'s, the lowest rung:
a steward or a discovery page needs no row for it. The lifecycle (protection, restore, drop) is `modifier`'s, which
holds the writer's and reader's rungs and no grant authority: giving access, taking it back and seeing who holds it
stay with whoever manages the object's grants.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own: alice makes `lance_catalog$db` and `lance_catalog$db$t` through their doors, so she owns both, and grants bob
`describer` and carol `modifier` on the table through its access door.
"""

from typing import Any

import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's code for a refused caller (`lance_docs/ns_catalog/spec.yaml`).
PERMISSION_DENIED = 15

TABLE = "lance_catalog$db$t"


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """alice's `lance_catalog$db$t` holding one row, bob its describer and carol its modifier."""
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    created = catalog.client.post(f"/v1/table/{TABLE}/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    for person, rung in (("bob", "describer"), ("carol", "modifier")):
        granted = catalog.client.post(f"/management/v1/table/{TABLE}/access/grant", json={"user": f"dex~{person}", "relation": rung}, headers=alice)
        assert granted.status_code == 200, granted.text
    return catalog


def test_a_describer_lists_and_describes_a_table_and_reads_no_row_and_gets_no_credential(estate: Any) -> None:  # noqa: ANN401
    """Its commit log is the describer's too, less the one field that is data: a delete's predicate names row values,
    so alice, who reads the rows, is shown it and bob is not."""
    bob, alice = estate.bearer("bob"), estate.bearer("alice")
    assert estate.client.post(f"/v1/table/{TABLE}/delete", json={"predicate": "id = 1"}, headers=alice).status_code == 200

    listed = estate.client.get("/v1/namespace/lance_catalog$db/table/list", headers=bob)
    described = estate.client.post(f"/v1/table/{TABLE}/describe", json={}, headers=bob)
    counted = estate.client.post(f"/v1/table/{TABLE}/count_rows", json={}, headers=bob)
    vended = estate.client.post(f"/management/v1/table/{TABLE}/credentials", json={}, headers=bob)
    history = {
        who: estate.client.get(f"/management/v1/table/{TABLE}/history", headers=bearer).json()["versions"][0]
        for who, bearer in (("bob", bob), ("alice", alice))
    }

    assert (listed.status_code, listed.json().get("tables")) == (200, ["t"]), listed.text
    assert (described.status_code, bool(described.json().get("location"))) == (200, True), described.text
    assert (counted.status_code, counted.json().get("code")) == (403, PERMISSION_DENIED), counted.text
    assert (vended.status_code, vended.json().get("code")) == (403, PERMISSION_DENIED), vended.text
    assert (history["bob"]["operation"], history["bob"].get("predicate"), history["alice"].get("predicate")) == ("Delete", None, "id = 1")


def test_a_modifier_runs_the_lifecycle_and_neither_grants_nor_sees_who_holds_what(estate: Any) -> None:  # noqa: ANN401
    carol = estate.bearer("carol")

    armed = estate.client.post(f"/management/v1/table/{TABLE}/protection", json={"protected": True}, headers=carol)
    cleared = estate.client.post(f"/management/v1/table/{TABLE}/protection", json={"protected": False}, headers=carol)
    granted = estate.client.post(f"/management/v1/table/{TABLE}/access/grant", json={"user": "dex~bob", "relation": "reader"}, headers=carol)
    holders = estate.client.post(f"/management/v1/table/{TABLE}/access/list", json={}, headers=carol)
    counted = estate.client.post(f"/v1/table/{TABLE}/count_rows", json={}, headers=carol)
    dropped = estate.client.post(f"/v1/table/{TABLE}/drop", json={}, headers=carol)

    assert (armed.status_code, cleared.status_code) == (200, 200), (armed.text, cleared.text)
    assert (granted.status_code, granted.json().get("code")) == (403, PERMISSION_DENIED), granted.text
    assert (holders.status_code, holders.json().get("code")) == (403, PERMISSION_DENIED), holders.text
    assert (counted.status_code, counted.text) == (200, "1"), counted.text
    assert dropped.status_code == 200, dropped.text


def test_a_rung_given_on_the_project_covers_a_table_of_its_warehouse(estate: Any) -> None:  # noqa: ANN401
    """One grant on a project reaches every warehouse of it, a table beneath included, and membership gives nothing:
    dave holds no rung on the table, and grace, an admin of the project, gives him `describer` on the project at its
    access door. A security admin of the project, who holds no data, is refused giving it."""
    dave = estate.bearer("dave")
    # No door makes a project's first admins or places the estate's own warehouse in a project: the estate's
    # declaration does, so they are seeded as it writes them.
    estate.store.write(
        (estate.person("grace"), "admin", "project:default"),
        (estate.person("erin"), "security_admin", "project:default"),
        ("project:default", "project", estate.ESTATE),
    )
    before = estate.client.post(f"/v1/table/{TABLE}/describe", json={}, headers=dave)

    granted = estate.client.post(
        "/management/v1/projects/default/access/grant", json={"user": "dex~dave", "relation": "describer"}, headers=estate.bearer("grace")
    )
    refused = estate.client.post(
        "/management/v1/projects/default/access/grant", json={"user": "dex~dave", "relation": "modifier"}, headers=estate.bearer("erin")
    )
    described = estate.client.post(f"/v1/table/{TABLE}/describe", json={}, headers=dave)
    counted = estate.client.post(f"/v1/table/{TABLE}/count_rows", json={}, headers=dave)

    assert (before.status_code, before.json().get("code")) == (403, PERMISSION_DENIED), before.text
    assert (granted.status_code, refused.status_code, refused.json().get("code")) == (200, 403, PERMISSION_DENIED), (granted.text, refused.text)
    assert (described.status_code, counted.status_code, counted.json().get("code")) == (200, 403, PERMISSION_DENIED), (described.text, counted.text)
