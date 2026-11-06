"""Arming or clearing deletion protection on a table or a namespace is the owner's act, never a writer's (#73, [[LH-055]]).

Protection is a statement about the object's destruction, so its door clears the drop's own bar: a writer who could arm
or clear it would disarm a safety that guards a destruction they could never perform. The table and namespace doors
each ask `can_set_protection`, a computed userset over `owner`.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]): alice makes `db` and `db$t` through their doors, so she owns both, and grants bob `writer` on `db`, the
rung that writes the table's rows.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's code for a refused caller (`lance_docs/ns_catalog/spec.yaml`).
PERMISSION_DENIED = 15


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """alice's `db` and `db$t`, with bob a writer on `db`."""
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    assert catalog.client.post("/v1/namespace/db/create", json={}, headers=alice).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    created = catalog.client.post("/v1/table/db$t/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    granted = catalog.client.post("/management/v1/namespace/db/access/grant", json={"user": "dex~bob", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    return catalog


@pytest.mark.parametrize(
    "door",
    [
        pytest.param("/management/v1/table/db$t/protection", id="the-table"),
        pytest.param("/management/v1/namespace/db/protection", id="the-namespace"),
    ],
)
def test_a_writer_is_refused_arming_protection(estate: Any, door: str) -> None:  # noqa: ANN401
    answer = estate.client.post(door, json={"protected": True}, headers=estate.bearer("bob"))

    assert (answer.status_code, answer.json().get("code")) == (403, PERMISSION_DENIED), answer.text
    read = estate.client.get(door, headers=estate.bearer("alice"))
    assert (read.status_code, read.json()["protected"]) == (200, False), read.text
