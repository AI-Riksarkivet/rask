"""Arming or clearing deletion protection on a table or a namespace is the owner's act, never a writer's (#73, [[LH-055]]).

Protection is a statement about the object's destruction, so its door clears the drop's own bar: a writer who could arm
or clear it would disarm a safety that guards a destruction they could never perform. The table and namespace doors
each ask `can_set_protection`, a computed userset over `owner`.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]): alice makes `lance_catalog$db` and `lance_catalog$db$t` through their doors, so she owns both, and grants bob `writer` on `lance_catalog$db`, the
rung that writes the table's rows.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.audit import AUDIT_LOGGER


#: The Lance Namespace spec's code for a refused caller (`lance_docs/ns_catalog/spec.yaml`).
PERMISSION_DENIED = 15


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """alice's `lance_catalog$db` and `lance_catalog$db$t`, with bob a writer on `lance_catalog$db`."""
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    created = catalog.client.post("/v1/table/lance_catalog$db$t/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    granted = catalog.client.post("/management/v1/namespace/lance_catalog$db/access/grant", json={"user": "dex~bob", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    return catalog


@pytest.mark.parametrize(
    "door",
    [
        pytest.param("/management/v1/table/lance_catalog$db$t/protection", id="the-table"),
        pytest.param("/management/v1/namespace/lance_catalog$db/protection", id="the-namespace"),
    ],
)
def test_a_writer_is_refused_arming_protection(estate: Any, door: str, caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN401
    """The trail records the relation and not the route, so the owner's read of the flag is asked on a relation of its
    own, `can_get_protection`: a read is never recorded as an arming ([[LH-585]])."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)

    answer = estate.client.post(door, json={"protected": True}, headers=estate.bearer("bob"))

    assert (answer.status_code, answer.json().get("code")) == (403, PERMISSION_DENIED), answer.text
    read = estate.client.get(door, headers=estate.bearer("alice"))
    assert (read.status_code, read.json()["protected"]) == (200, False), read.text
    asked = [(r.__dict__["audit.subject"], r.__dict__.get("audit.relation", ""), r.__dict__["audit.outcome"]) for r in caplog.records if r.name == AUDIT_LOGGER]
    assert [verdict for verdict in asked if "protection" in verdict[1]] == [
        ("dex~bob", "can_set_protection", "deny"),
        ("dex~alice", "can_get_protection", "allow"),
    ]
