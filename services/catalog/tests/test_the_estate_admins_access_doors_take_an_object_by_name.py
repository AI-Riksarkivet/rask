"""The estate admin's raw access doors take a table or namespace by name as they take it by uuid ([[LH-591]]).

Every grant is held by the object's uuid (``table:<uuid>``, ``namespace:<uuid>``, [[LH-524]]), and every catalog door is
addressed by name and resolves it through the request's ``Resolver``. A door handing OpenFGA ``table:db$t`` as written
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
    """root administers the estate; alice owns namespace ``db`` and table ``db$t``, on which the raw door grants bob ``reader`` by name."""
    catalog = governed_catalog
    catalog.store.write((fga.user_object("dex~root"), "owner", "estate:rask"))
    _ok(catalog, "/v1/namespace/db/create", json={})
    _ok(catalog, "/v1/table/db$t/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    _ok(catalog, f"{ACCESS}/tuples", "root", json={"user": catalog.person("bob"), "relation": "reader", "object": "table:db$t"})
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
    [pytest.param("table", "db$t", "db$gone", TABLE_NOT_FOUND, id="table"), pytest.param("namespace", "db", "gone", NAMESPACE_NOT_FOUND, id="namespace")],
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
