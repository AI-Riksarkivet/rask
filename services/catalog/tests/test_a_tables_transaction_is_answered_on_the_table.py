"""A table's transaction is answered on the table: describe at its reader rung, alter declined past its writer rung ([[LH-221]]).

The `dir` backend names a transaction `<table id>$<transaction>`, the transaction being its uuid or the version it
committed, and refuses an id that names no table (measured on pylance 13.0.0). So both doors authorize the table the id
names, as the object its name resolves to: describing a transaction reads the table's metadata (`can_get_metadata`), and
altering one would write it (`can_write_data`). An id of one segment names no table and is refused 400 before any grant
is asked.

AlterTransaction is all or nothing (`lance_docs/ns_catalog/spec.yaml`: "either succeed and apply all actions, or fail and
apply no action"). On pylance 13.0.0 the `dir` backend answers SUCCEEDED to a status change and to a property and applies
neither, so the catalog declines the door with the spec's Unsupported (406) rather than pass a 200 that applied nothing.

Every decision is the real model's, on a store of this test's own: alice creates `db1` and `db1$t` through the doors,
which seed her ownership, and grants bob `reader` on the table. Carol holds nothing.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's error codes a stock client reads (`lance_docs/ns_catalog/spec.yaml`).
UNSUPPORTED, INVALID_INPUT, PERMISSION_DENIED = 0, 13, 15

#: The transaction the table's create committed, named by the version it made.
TRANSACTION = "db1$t$1"

CANCEL = {"actions": [{"setStatusAction": {"status": "Canceled"}}]}


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """`db1$t`, which alice owns and bob reads."""
    alice = governed_catalog.bearer("alice")
    client = governed_catalog.client
    assert client.post("/v1/namespace/db1/create", json={}, headers=alice).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    created = client.post("/v1/table/db1$t/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    granted = client.post("/management/v1/table/db1$t/access/grant", json={"user": "dex~bob", "relation": "reader"}, headers=alice)
    assert granted.status_code == 200, granted.text
    return governed_catalog


@pytest.mark.parametrize("caller", ["alice", "bob"], ids=["the-owner", "a-reader"])
def test_a_holder_of_the_tables_read_rung_describes_its_transaction(estate: Any, caller: str) -> None:
    answered = estate.client.post(f"/v1/transaction/{TRANSACTION}/describe", json={}, headers=estate.bearer(caller))

    assert answered.status_code == 200, answered.text
    assert (answered.json()["status"].lower(), answered.json()["properties"]["version"]) == ("succeeded", "1")


@pytest.mark.parametrize(
    ("caller", "door", "body", "status", "code"),
    [
        pytest.param("carol", "describe", {}, 403, PERMISSION_DENIED, id="describe-refuses-a-caller-holding-nothing-on-the-table"),
        pytest.param("alice", "alter", CANCEL, 406, UNSUPPORTED, id="alter-is-declined-past-the-writer-rung"),
        pytest.param("bob", "alter", CANCEL, 403, PERMISSION_DENIED, id="alter-refuses-a-reader"),
    ],
)
def test_a_transaction_door_answers_on_the_tables_rung(estate: Any, caller: str, door: str, body: dict[str, Any], status: int, code: int) -> None:
    answered = estate.client.post(f"/v1/transaction/{TRANSACTION}/{door}", json=body, headers=estate.bearer(caller))

    assert (answered.status_code, answered.json()["code"]) == (status, code), answered.text


@pytest.mark.parametrize("door", ["describe", "alter"])
def test_a_transaction_id_naming_no_table_is_refused_before_any_grant(estate: Any, door: str) -> None:
    answered = estate.client.post(f"/v1/transaction/1/{door}", json=CANCEL, headers=estate.bearer("alice"))

    assert (answered.status_code, answered.json()["code"]) == (400, INVALID_INPUT), answered.text
