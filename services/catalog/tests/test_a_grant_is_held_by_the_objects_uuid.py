"""A grant is held by the table's or namespace's uuid, so a rename moves none and a re-created name inherits none.

The catalog mints each table's and namespace's uuid when it comes into being and keeps it across a rename (ADR 0173 §2),
and every FGA object it writes is ``table:<uuid>`` or ``namespace:<uuid>``, its parent and child edges joining uuids
(ADR 0173 §8, [[LH-524]]). The doors stay addressed by name and resolve the uuid on the server (ADR 0173 §11). So:

- a reader granted on ``t`` reads it under its new name ``u``, and the rename writes no tuple;
- a table destroyed and created again under that name is another object, and the reader holds nothing on it;
- no name reaches OpenFGA, so a name OpenFGA could not hold as an object id (a space) is created and described.

Every decision is the real model's, on a store of this test's own ([[LH-235]]); every tuple is the one the door writes.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))
_WAREHOUSE = "warehouse:lance_catalog"


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    answer = catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


def _create_table(catalog: Any, table: str) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    _ok(catalog, f"/v1/table/{table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})


def _uuid(catalog: Any, kind: str, name: str) -> str:  # noqa: ANN401 — the conftest's GovernedCatalog
    return str(_ok(catalog, f"/v1/{kind}/{name}/describe", json={})["properties"]["rask.uuid"])


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's namespace ``db`` holding ``db$t``, which bob is granted ``reader`` on through the grant door."""
    _ok(governed_catalog, "/v1/namespace/db/create", json={})
    _create_table(governed_catalog, "db$t")
    _ok(governed_catalog, "/management/v1/table/db$t/access/grant", json={"user": "dex~bob", "relation": "reader"})
    return governed_catalog


def test_a_reader_keeps_a_renamed_table_and_holds_nothing_on_one_created_again_under_its_name(estate: Any) -> None:  # noqa: ANN401
    held = estate.store.tuples()

    _ok(estate, "/v1/table/db$t/rename", json={"new_table_name": "u"})

    assert estate.store.tuples() == held, "the rename wrote or removed a tuple"
    assert estate.client.post("/v1/table/db$u/describe", json={}, headers=estate.bearer("bob")).status_code == 200, "bob lost the renamed table"
    _ok(estate, "/v1/table/db$u/drop", json={})
    _create_table(estate, "db$u")
    refused = estate.client.post("/v1/table/db$u/describe", json={}, headers=estate.bearer("bob"))
    assert refused.status_code == 403, f"bob reads a table created under the name he was granted: {refused.text}"


def test_every_tuple_a_create_writes_names_the_objects_uuid(estate: Any) -> None:  # noqa: ANN401
    _ok(estate, "/v1/namespace/db$inner/create", json={})
    _ok(estate, "/v1/table/db$inner$declared/declare", json={})
    namespace, inner = _uuid(estate, "namespace", "db"), _uuid(estate, "namespace", "db$inner")
    table, declared = _uuid(estate, "table", "db$t"), _uuid(estate, "table", "db$inner$declared")
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
    _create_table(estate, "db$sp ace")

    described = _ok(estate, "/v1/table/db$sp ace/describe", json={})
    assert (estate.person("alice"), "owner", f"table:{described['properties']['rask.uuid']}") in estate.store.tuples()
