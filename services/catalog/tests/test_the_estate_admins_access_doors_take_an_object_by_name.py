"""The estate admin's raw access doors take a table or namespace by name as they take it by uuid ([[LH-591]]), and a project
or a warehouse by its id as every door names it (ADR 0245, review T5).

Every grant is held by the object's uuid (``table:<uuid>``, ``namespace:<uuid>``, [[LH-524]]), and every catalog door is
addressed by name and resolves it through the request's ``Resolver``. A door handing OpenFGA ``table:lance_catalog$db$t`` as written
would ask about an object no tuple names, and its check would answer denied whoever reads the table. So check,
list-users, expand and simulate resolve a name as every door does, and answer the uuid object they asked about with its
name beside it. The tuple door's names are pinned beside its filters
(``test_the_tuple_browser_answers_only_a_filter_openfga_reads.py``).

Every answer is the real OpenFGA's, on a store of this test's own; every tuple is written by the door that writes it.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga


ACCESS = "/management/v1/access"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))
#: The spec's codes for an absent table and an absent namespace (`lance_docs/ns_catalog/spec.yaml`).
TABLE_NOT_FOUND, NAMESPACE_NOT_FOUND = 4, 1


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> dict[str, Any]:
    answer = catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


def _uuid(catalog: Any, kind: str, name: str) -> str:
    return str(_ok(catalog, f"/v1/{kind}/{name}/describe", json={})["properties"]["rask.uuid"])


@pytest.fixture
def estate(governed_catalog: Any) -> Any:
    """root administers the estate; alice owns namespace ``lance_catalog$db`` and table ``lance_catalog$db$t``, on which the raw door grants bob ``reader`` by name."""
    catalog = governed_catalog
    catalog.store.write((fga.user_object("dex~root"), "owner", "estate:rask"))
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    _ok(catalog, "/v1/table/lance_catalog$db$t/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    _ok(catalog, f"{ACCESS}/tuples", "root", json={"user": catalog.person("bob"), "relation": "reader", "object": "table:lance_catalog$db$t"})
    return catalog


def _asking(door: str, obj: str, catalog: Any) -> dict[str, Any]:
    """The body ``door`` takes, asking whether bob sees ``obj`` (or, for simulate, whether carol would once granted ``reader``)."""
    match door:
        case "check":
            return {"user": catalog.person("bob"), "relation": "can_get_metadata", "object": obj}
        case "list-users":
            return {"relation": "can_get_metadata", "object": obj}
        case "expand":
            return {"relation": "can_get_metadata", "object": obj, "depth": 3}
        case _:
            carol = catalog.person("carol")
            return {"user": carol, "relation": "can_get_metadata", "object": obj, "hypothetical": [{"user": carol, "relation": "reader", "object": obj}]}


@pytest.mark.parametrize("door", ["check", "list-users", "expand", "simulate"])
@pytest.mark.parametrize(
    ("kind", "name", "absent", "code"),
    [
        pytest.param("table", "lance_catalog$db$t", "lance_catalog$db$gone", TABLE_NOT_FOUND, id="table"),
        pytest.param("namespace", "lance_catalog$db", "lance_catalog$gone", NAMESPACE_NOT_FOUND, id="namespace"),
    ],
)
def test_a_door_answers_an_object_by_name_as_it_answers_it_by_uuid(estate: Any, door: str, kind: str, name: str, absent: str, code: int) -> None:
    uuid_object = f"{kind}:{_uuid(estate, kind, name)}"

    by_name = _ok(estate, f"{ACCESS}/{door}", "root", json=_asking(door, f"{kind}:{name}", estate))

    assert by_name == _ok(estate, f"{ACCESS}/{door}", "root", json=_asking(door, uuid_object, estate))
    named = by_name["checked"] if door in ("check", "simulate") else by_name
    assert (named["object"], named["name"]) == (uuid_object, name)
    assert by_name.get("allowed", by_name.get("tree") or by_name.get("users")), f"{door} answered nothing on an object bob sees"
    refused = estate.client.post(f"{ACCESS}/{door}", json=_asking(door, f"{kind}:{absent}", estate), headers=estate.bearer("root"))
    assert (refused.status_code, refused.json()["code"]) == (404, code)
    # alice owns the objects asked about and holds nothing on the estate: the door is the estate admin's alone.
    kept_out = estate.client.post(f"{ACCESS}/{door}", json=_asking(door, uuid_object, estate), headers=estate.bearer("alice"))
    assert (kept_out.status_code, kept_out.json()["detail"]) == (403, "can_administer_authz required on estate:rask")


def test_a_door_refuses_a_warehouse_named_as_a_namespace(estate: Any) -> None:
    """A namespace name of one segment is a warehouse ([[LH-593]]), asked about under its own type, never as a namespace."""
    refused = estate.client.post(f"{ACCESS}/check", json=_asking("check", "namespace:lance_catalog", estate), headers=estate.bearer("root"))

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"]) == (400, "application/problem+json", 13), refused.text


@pytest.fixture
def acme(estate: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """root, the estate's admin too, makes project ``acme`` and makes bob a member of it through the raw tuple door, naming
    the project by its id."""
    estate.store.write((fga.user_object("dex~root"), "admin", "estate:rask"))
    _ok(estate, "/management/v1/projects", "root", json={"id": "acme"})
    _ok(estate, f"{ACCESS}/tuples", "root", json={"user": estate.person("bob"), "relation": "member", "object": "project:acme"})
    return estate


@pytest.mark.parametrize("door", ["check", "list-users", "expand", "simulate"])
def test_a_door_answers_a_project_by_its_id_as_it_answers_its_creation(acme: Any, door: str) -> None:  # noqa: ANN401
    """The tools speak ids: ``project:acme`` is asked about on the creation the id names, and answered with the id beside
    it; an id no project holds is the caller's input, 400, never a 500 and never a question about an object nothing names."""
    creation = acme.creation("project", "acme")
    asking = {**_asking(door, "project:acme", acme), "relation": "member"}
    if door == "simulate":
        asking["hypothetical"] = [{"user": acme.person("carol"), "relation": "member", "object": "project:acme"}]

    by_id = _ok(acme, f"{ACCESS}/{door}", "root", json=asking)

    named = by_id["checked"] if door in ("check", "simulate") else by_id
    assert (named["object"], named["name"]) == (creation, "acme")
    assert by_id.get("allowed", by_id.get("tree") or by_id.get("users")), f"{door} answered nothing on a project bob is a member of"
    nowhere = {**asking, "object": "project:nope"}
    refused = acme.client.post(f"{ACCESS}/{door}", json=nowhere, headers=acme.bearer("root"))
    assert (refused.status_code, refused.json()["code"]) == (400, 13), refused.text


def test_the_tuple_doors_write_read_and_delete_a_projects_tuple_by_its_id(acme: Any) -> None:  # noqa: ANN401
    creation, bob = acme.creation("project", "acme"), acme.person("bob")

    read = acme.client.get(f"{ACCESS}/tuples", params={"object": "project:acme"}, headers=acme.bearer("root"))
    nowhere = acme.client.post(f"{ACCESS}/tuples", json={"user": bob, "relation": "member", "object": "project:nope"}, headers=acme.bearer("root"))
    deleted = acme.client.request("DELETE", f"{ACCESS}/tuples", json={"user": bob, "relation": "member", "object": "project:acme"}, headers=acme.bearer("root"))

    assert read.status_code == 200, read.text
    assert {(t["user"], t["object"], t["name"]) for t in read.json()["tuples"] if t["relation"] == "member"} == {(bob, creation, "acme")}
    assert (nowhere.status_code, nowhere.json()["code"]) == (400, 13), nowhere.text
    assert (deleted.status_code, (bob, "member", creation) in acme.store.tuples()) == (200, False), deleted.text
